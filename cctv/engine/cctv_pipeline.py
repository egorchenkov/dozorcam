#!/usr/bin/env python3
"""Recorder and motion detector for CCTV Bridge v1."""
from __future__ import annotations

import collections
import datetime
import json
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.parse

from .. import settings
from .cctv_bridge import Bridge, Camera, check_events_url, now
from .onvif_motion_gate import OnvifMotionGate
from .model_switch import ModelManager
from .threshold_calibration import ThresholdManager
from .still_object_filter import StillObjectFilter

# cv2 открывает RTSP тем же ffmpeg: транспорт задаётся только через это окружение
# и должен совпадать с recorder, иначе frame-diff молча деградирует в fallback.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

try:
    import cv2
except ImportError:
    # Без opencv детектор молча крутится вхолостую — юнит обязан идти venv-питоном.
    print("motion_disabled reason=opencv_missing", flush=True)
    cv2 = None

REDACT = re.compile(r"rtsp://[^\s'\"]*")

# Раз в столько секунд рядом с живым ffmpeg прогоняется retention-guard и
# подрезается буфер. До 25.09.2026 ради этого ffmpeg перезапускали каждый цикл:
# каждый перезапуск — дыра в записи 7–9 с (5–6 % времени) и, на dacha3, новая
# RTSP-сессия с середины GOP: первый пакет без метки времени, mpegts-муксер
# ронял ffmpeg («first pts and dts value must be set»), 138 падений за сутки —
# все на старте сессии, ни одного посреди цикла. Теперь ffmpeg живёт, пока
# жив поток; перезапуск — только по ошибке потока или по «залипанию».
RECORDER_CYCLE_SEC = float(os.environ.get("CCTV_RECORDER_CYCLE_SEC", "130"))
# Сколько ждём, пока ffmpeg закроет текущий сегмент по SIGTERM.
RECORDER_STOP_GRACE_SEC = float(os.environ.get("CCTV_RECORDER_STOP_GRACE_SEC", "10"))
# Залипание: сокет жив, а новых сегментов нет. -timeout у RTSP ловит только
# молчание сокета, поэтому проверяем свежесть последнего сегмента сами.
RECORDER_STALL_SEC = float(os.environ.get("CCTV_RECORDER_STALL_SEC", "45"))


DETECT_BUFFER_SUFFIX = ".detect"


def detect_buffer_name(camera: Camera) -> str:
    """Каталог буфера, который читает детектор людей: main или записанный substream."""
    uses_substream = camera.detect_substream and camera.detect_rtsp_url
    return camera.camera_id + (DETECT_BUFFER_SUFFIX if uses_substream else "")


def record(camera: Camera, storage: pathlib.Path, source: str | None = None, name: str | None = None) -> None:
    """Five-second TS segments; only own oldest segments are overwritten."""
    target = settings.engine_buffer(storage) / (name or camera.camera_id)
    target.mkdir(parents=True, exist_ok=True)
    # Одна проба на старт процесса, до первой сессии рекордера: камере
    # (особенно dacha3) каждая лишняя параллельная RTSP-сессия роняет поток.
    audio_copy = source is None and probe_audio_codec(camera.rtsp_url) == "aac"
    while True:
        maintain_buffer(target)
        # Буфер детектора — только картинка: звук ему не нужен, а перекодирование
        # в AAC — лишняя работа на каждой камере.
        command, env = segment_command(source or camera.rtsp_url, target, audio=source is None,
                                       audio_copy=audio_copy)
        try:
            run_recorder(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env),
                         camera, target)
        except OSError as error: report(camera, str(error).encode(), buffer=target.name)
        prune_buffer(target); time.sleep(2)


# Сторож диска лежит в scripts/ рядом с пакетом; в образе путь задаётся явно.
RETENTION_GUARD = os.environ.get("CCTV_RETENTION_GUARD") or str(
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "retention-guard.sh")


def maintain_buffer(target: pathlib.Path) -> None:
    """Обслуживание буфера, которое раньше требовало перезапуска ffmpeg: guard читает
    только du/df, prune удаляет старые сегменты — живому ffmpeg они не мешают."""
    if RETENTION_GUARD and os.access(RETENTION_GUARD, os.X_OK):
        subprocess.run([RETENTION_GUARD, "check"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prune_buffer(target)


def newest_segment_age(target: pathlib.Path, now: float | None = None) -> float | None:
    """Сколько секунд назад менялся самый свежий сегмент; None — сегментов нет."""
    stamps = [p.stat().st_mtime for p in target.glob("*.ts") if p.exists()]
    return ((now if now is not None else time.time()) - max(stamps)) if stamps else None


def run_recorder(process: subprocess.Popen, camera: Camera, target: pathlib.Path) -> None:
    """Держать ffmpeg, пока жив поток: обслуживание буфера идёт рядом, а не через
    перезапуск. Выход — сам ffmpeg завершился (ошибка потока: сообщаем причину)
    или залип (сегменты не обновляются): тогда закрываем его по-хорошему."""
    started = time.time()
    while True:
        try:
            _, stderr = process.communicate(timeout=RECORDER_CYCLE_SEC)
        except subprocess.TimeoutExpired:
            maintain_buffer(target)
            age = newest_segment_age(target)
            # Первому сегменту даём цикл на появление: сразу после старта файлов ещё нет.
            stalled = (age if age is not None else time.time() - started) > RECORDER_STALL_SEC
            if not stalled:
                continue
            report(camera, f"no fresh segment for {RECORDER_STALL_SEC:.0f}s — restarting recorder", buffer=target.name)
            stop_recorder(process, camera)
            return
        if process.returncode: report(camera, stderr, buffer=target.name)
        return


def stop_recorder(process: subprocess.Popen, camera: Camera) -> None:
    """Закрыть ffmpeg по-хорошему: SIGTERM он обрабатывает сам — дописывает и
    закрывает текущий сегмент. SIGKILL (так делал ``subprocess.run(timeout=...)``)
    оставлял открытый .ts обрезанным, следующий ffmpeg сразу создавал более новый
    файл — и для детектора обрезок выглядел закрытым: пачка ``[h264] bytestream -N``
    и рваный кадр в тревоге. SIGKILL — только если SIGTERM не подействовал.
    """
    process.terminate()
    try:
        process.communicate(timeout=RECORDER_STOP_GRACE_SEC)
    except subprocess.TimeoutExpired:
        # Не закрылся сам — сегмент всё равно будет рваным, но висящий ffmpeg хуже.
        report(camera, "recorder did not stop on SIGTERM")
        process.kill(); process.communicate()


def probe_audio_codec(source: str) -> str | None:
    """Кодек звука камеры одной короткой ffprobe-сессией; ошибка/таймаут — None."""
    if not source.startswith("rtsp://"):
        return None
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "a:0",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0", source],
                               capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return probe.stdout.strip().split(",")[0] or None


def segment_command(source: str, target: pathlib.Path, audio: bool = True,
                    audio_copy: bool = False) -> tuple[list[str], dict[str, str]]:
    """Команда и окружение записи сегментов; выделены, чтобы тест проверял их напрямую."""
    # -timeout, не -rw_timeout: последней опции у RTSP-демуксера нет и ffmpeg выходил мгновенно.
    # TCP-транспорт задан явно — UDP через WireGuard ненадёжен. Обе опции только для RTSP:
    # на файловом источнике демуксер их не принимает.
    transport = ["-rtsp_transport", "tcp", "-timeout", "5000000"] if source.startswith("rtsp://") else []
    # Звук (решение владельца 23.09.2026: клипы со звуком). Камеры отдают кто G.711, кто MP3,
    # кто AAC; G.711 MPEG-TS не принимает (dacha3 сыпал «Error muxing a packet», dacha
    # писал звук как bin_data), поэтому звук всегда перекодируем в AAC — он же нужен MP4
    # клипа. Видео не трогаем. «?» — камеры без звука пишутся как раньше; явный -map
    # заодно отсекает data-дорожку ONVIF-метаданных. Буферу детектора звук не нужен.
    #
    # Камеру, которая сама отдаёт AAC (dacha2, dacha3), не перекодируем: на
    # dacha3 после битого NAL от камеры перекодированный аудиокадр выходил без
    # временной метки, и mpegts-муксер нового сегмента ронял ffmpeg («first pts
    # and dts value must be set» → «Error muxing a packet», до 70 раз в сутки,
    # замер 25.09.2026). Скопированный пакет несёт RTP-метку камеры; genpts,
    # wallclock и asetpts на перекодировании не помогали (проверено в тот же день).
    if audio and audio_copy:
        sound = ["-map", "0:a:0?", "-c:a", "copy"]
    else:
        sound = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "48k"] if audio else ["-an"]
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", *transport, "-i", source,
               "-map", "0:v:0", *sound, "-c:v", "copy",
               "-f", "segment", "-segment_time", f"{RECORDED_SEGMENT_SEC:g}", "-strftime", "1", "-reset_timestamps", "1",
               str(target / "%Y-%m-%dT%H:%M:%SZ.ts")]
    # TZ=UTC обязателен: ffmpeg -strftime пишет ЛОКАЛЬНОЕ время, а clip() читает имя
    # сегмента как UTC — без этого окно ±15 с всегда пустое и клип не собирается.
    return command, dict(os.environ, TZ="UTC")


def report(camera: Camera, stderr: bytes | str, buffer: str | None = None) -> None:
    """Ошибку записи видно в журнале; RTSP URL с credential наружу не выводится."""
    text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    # Последние строки, а не одна: «Error muxing a packet» — следствие, причина
    # (битый NAL, пакет без pts, отказ муксера) стоит на 1–3 строки выше, и без
    # них разбор dacha3 25.09.2026 шёл вслепую.
    lines = [line[:160] for line in REDACT.sub("rtsp://<redacted>", text).strip().splitlines()[-4:]] or ["no output"]
    # buffer= различает main-рекордер и рекордер substream детектора одной камеры.
    where = f" buffer={buffer}" if buffer else ""
    print(f"recorder_failed camera={camera.camera_id}{where} reason={' | '.join(lines)}", flush=True)


# 24 сегмента по 5 с давали всего 2 минуты истории: кнопка «Клип вокруг кадра»
# под уже отправленным фото почти всегда приходила к пустому окну ±15 с.
# Порог «размазанного» кадра (замер 23.09.2026 на dacha3: у рваного кадра
# 100 % нижних строк совпадают с верхней соседкой, у целых — 0 %).
SMEAR_ROW_DIFF = 0.5
SMEAR_COLUMN_DIFF = 2.0
SMEAR_SHARE = 0.5
BUFFER_SEGMENTS = int(os.environ.get("CCTV_BUFFER_SEGMENTS", "120"))  # 120 x 5 c = 10 минут
BUFFER_MAX_BYTES = int(os.environ.get("CCTV_BUFFER_MAX_BYTES", str(2 * 1024**3)))
# Порог в процентах. На целом substream шум покоя ничтожен (замеры motion_stats
# 24.08.2026: p95 = 0.09 %, max = 0.10 %), 1 % — десятикратный запас. Прежние 3 %
# мерились на рваном потоке (распад картинки, а не сцена) и на целом substream
# пропускали бы фигуру почти во весь кадр. От одиночного всплеска защищает не
# порог, а требование двух замеров подряд.
MOTION_THRESHOLD = float(os.environ.get("CCTV_MOTION_THRESHOLD", "1"))
# Пауза между алертами: 15 с давали очередь пушей на одно и то же событие.
MOTION_COOLDOWN = float(os.environ.get("CCTV_MOTION_COOLDOWN", "60"))
# Кадры между замерами не выбрасываются, а вычитываются: иначе буфер сессии
# растёт и детектор сравнивает всё более старую картинку.
DETECT_INTERVAL = float(os.environ.get("CCTV_DETECT_INTERVAL", "0.4"))
DETECT_WARMUP = int(os.environ.get("CCTV_DETECT_WARMUP", "8"))
# Раз в минуту — разброс замеров в журнал: по нему видно и шум покоя, и амплитуду
# настоящего движения; без него порог настраивался вслепую.
STATS_INTERVAL = float(os.environ.get("CCTV_MOTION_STATS_INTERVAL", "60"))
# YOLO — единственная дорогая часть пилота (~80 мс/кадр на этом хосте, 5 кадров/с
# на каждой камере круглосуточно), поэтому перед ним стоит гейт. Первым кандидатом
# был ONVIF-motion самих камер: подписка живёт на обеих (docs/why-server-side-detection.md).
# Выключен по умолчанию по результату замера 04.09.2026: на даче картинка
# была изменена целиком (яркость 50->100, 97.7 % пикселей, средняя 114->222), и
# камера НЕ выдала ни одного motion-события. То есть VMD этой камеры как источник
# сигнала не работает, и гейт по нему тихо похоронил бы детекцию людей. На
# городской камеры проверить не удалось: она молча игнорирует запись imaging под
# ONVIF-стор аккаунтом (картинка не изменилась, тест недействителен).
# Код оставлен: он рабочий, подписка живёт на обеих камерах.
MOTION_GATE_ENABLED = os.environ.get("CCTV_MOTION_GATE_ENABLED", "0") != "0"
# Держим гейт открытым дольше одного кадра: буфер пишет 5-секундными сегментами,
# и после реального события ещё нужно окно на «два кадра подряд» YOLO.
MOTION_GATE_HOLD_SECONDS = float(os.environ.get("CCTV_MOTION_GATE_HOLD_SECONDS", "20"))
# shadow — гейт считает решение и пишет его в журнал, но НЕ пропускает кадры мимо
# YOLO; enforce — решение исполняется. Умолчание shadow осознанное: до сих пор не
# доказано, что VMD обеих камер вообще армирован. Если он выключен в настройках
# камеры, IsMotion не станет true никогда, и enforce тихо похоронил бы рабочую
# детекцию людей. Shadow как раз измеряет и выигрыш, и цену промаха без риска.
MOTION_GATE_MODE = os.environ.get("CCTV_MOTION_GATE_MODE", "shadow")
# Окно вокруг сегмента: движение могло начаться прямо перед его началом и быть
# отмечено камерой уже после — сегмент длится 5 с, сигнал может опоздать.
MOTION_GATE_PRE_SEC = float(os.environ.get("CCTV_MOTION_GATE_PRE_SEC", "10"))
MOTION_GATE_POST_SEC = float(os.environ.get("CCTV_MOTION_GATE_POST_SEC", "15"))
# Предохранитель от мёртвого VMD: даже при закрытом гейте раз в N секунд кадр
# всё равно смотрим. Это и телеметрия (person_stats не замолкает), и страховка —
# найденный на таком кадре человек сам открывает гейт (see forced_scan_until).
MOTION_GATE_KEEPALIVE_SEC = float(os.environ.get("CCTV_MOTION_GATE_KEEPALIVE_SEC", "120"))
# Гейт по ЛЮДЯМ от самой камеры. Hikvision G2/G5 (dacha2, dacha3) классифицируют
# цель в Smart/FieldDetection и отдают её по ONVIF топиком
# RuleEngine/FieldDetector/ObjectsInside — той же ONVIF-учёткой, что и snapshot
# (ISAPI alertStream, напротив, требует admin: 26.09.2026 обе камеры ответили 401).
# Сверка 25–26.09: ночью 528 VMD от куста, FieldDetection 0; единственный
# реальный проход (26.09 06:57) камера и YOLO увидели оба. Режимы:
#   off     — не подписываться;
#   shadow  — подписаться, считать «камера видела / молчала» на каждый кадр и
#             на каждое событие YOLO, но решений не принимать (по умолчанию);
#   enforce — без сигнала камеры YOLO не запускать (остаются keepalive и
#             forced_scan — предохранители от мёртвой подписки).
# Включается только на камерах с camera_human_events в реестре.
HUMAN_GATE_MODE = os.environ.get("CCTV_HUMAN_GATE_MODE", "shadow")
HUMAN_GATE_HOLD_SECONDS = float(os.environ.get("CCTV_HUMAN_GATE_HOLD_SECONDS", "20"))
# Окно вокруг момента съёмки кадра. Камера сообщает о входе в зону после
# timeThreshold (1 с) и с задержкой PullMessages; человек же остаётся в кадре
# десятки секунд, а повторный ObjectsInside=true камера может и не прислать,
# поэтому «после» заметно длиннее «до». Цена широкого окна в enforce — пара
# лишних десятков кадров YOLO на реальное событие, то есть ничто.
HUMAN_GATE_PRE_SEC = float(os.environ.get("CCTV_HUMAN_GATE_PRE_SEC", "15"))
HUMAN_GATE_POST_SEC = float(os.environ.get("CCTV_HUMAN_GATE_POST_SEC", "45"))
# Запас вокруг интервала active…inactive для телеметрии «по состоянию» (02.10.2026).
HUMAN_GATE_STATE_MARGIN_SEC = float(os.environ.get("CCTV_HUMAN_GATE_STATE_MARGIN_SEC", "60"))
# Рабочий гейт — собственный frame-diff по main-кадру: единицы миллисекунд против
# ~80 мс YOLO, не зависит ни от вендора, ни от настроек камеры, и это ровно тот
# алгоритм, который был штатным детектором движения до пилота людей. ONVIF-гейт
# выше может гейт только ОТКРЫТЬ, закрыть — никогда.
# Умолчание enforce (D-20261003-01): в shadow гейт только считает решение, а YOLO
# идёт на каждом выбранном кадре — 4 камеры × 2 к/с ≈ 2.5 ядра против ~0.23 в
# enforce. shadow остаётся режимом диагностики цены гейта.
PERSON_GATE_MODE = os.environ.get("CCTV_PERSON_GATE_MODE", "enforce")
# Порог намеренно много ниже тревожных (1 % у городской камеры, 2 % на даче): гейт
# обязан быть щедрым — пропустить кадр на YOLO дешевле, чем потерять человека.
# Значение уточняется по diff_stats на main-кадрах, см. журнал gate_stats.
PERSON_GATE_THRESHOLD = float(os.environ.get("CCTV_PERSON_GATE_THRESHOLD", "0.15"))
# Шум покоя — свойство сцены, а не узла. Замер ночью 21.09.2026: dacha3 держит
# frame-diff на p50 1.1 %, p95 2.2 % (усиление матрицы и ИК-подсветка), тогда как
# dacha2 в ту же минуту — 0.018 %. Общий порог 0.15 % на такой сцене не
# закрывается никогда (saved=0 %, CPU конвейера 340 %). Поднять его константой
# нельзя: днём шум той же камеры падает на два порядка, и ночное значение
# ослепило бы её до keepalive. Поэтому база берётся per-camera (registry
# person_gate_threshold), а сверх неё едет плавающий пол по медиане замеров.
PERSON_GATE_NOISE_FACTOR = float(os.environ.get("CCTV_PERSON_GATE_NOISE_FACTOR", "1.5"))
# Квантиль окна замеров, от которого считается пол. Медиана (до 25.09.2026)
# по построению пропускала на YOLO 40–70 % кадров шумной сцены (dacha днём,
# dacha3 с ИК-подсветкой): половина замеров всегда выше медианы, а фактор 1.5
# перекрывает лишь узкий хвост. Замер 25.09.2026 на буферах при 2 кадрах/с:
# p50×1.5 пропускает 37–39 %, p75×1.5 — 16–17 %, при этом порог (0.7–2.3 %)
# остаётся ниже фигуры человека (2–5 % пикселей между соседними выборками).
# Верхний квартиль устойчив к самому событию: человек в кадре четверть окна
# не сдвигает его. 600 замеров — пять минут при 2 кадрах/с.
PERSON_GATE_NOISE_QUANTILE = float(os.environ.get("CCTV_PERSON_GATE_NOISE_QUANTILE", "0.75"))
PERSON_GATE_NOISE_WINDOW = int(os.environ.get("CCTV_PERSON_GATE_NOISE_WINDOW", "600"))
# Потолок плавающего пола: рваный поток (распад картинки на макроблоки) даёт
# десятки процентов подряд, и без предела пол закрыл бы гейт совсем.
PERSON_GATE_NOISE_MAX = float(os.environ.get("CCTV_PERSON_GATE_NOISE_MAX", "3"))
PERSON_DETECT_INTERVAL = float(os.environ.get("CCTV_PERSON_DETECT_INTERVAL", "1"))
PERSON_HITS = int(os.environ.get("CCTV_PERSON_HITS", "2"))
# Незавершённый .ts ещё дописывается ffmpeg. Не открываем его на чтение: OpenCV
# может получить неполный GOP и либо не декодировать кадр, либо вывести в журнал
# ошибки H.264. Один стабильный файл добавляет не более ~6 с к тревоге.
#
# Готовность НЕЛЬЗЯ определять по mtime: ffmpeg сбрасывает TS крупными блоками
# (замер 21.09.2026 на dacha2 — 0.5-1 МБ за раз), поэтому у открытого файла
# mtime легко застывает на несколько секунд, и любая выдержка по времени
# однажды пропускает обрезанный сегмент. Признак закрытия у segment-муксера
# ровно один и он детерминированный: файл закрывается перед созданием
# следующего, значит сегмент готов тогда, когда есть более новый по имени.
# Выдержка осталась только как страховка от гонки на самом создании файла.
RECORDED_SEGMENT_SETTLE_SEC = float(os.environ.get("CCTV_RECORDED_SEGMENT_SETTLE_SEC", "1"))
# Длина сегмента рекордера (-segment_time в segment_command).
RECORDED_SEGMENT_SEC = 5.0
# Сколько после пропуска сегментов детектор считается «не успевает» (D-20261003-01):
# буфер удалил сегменты раньше, чем детектор до них дошёл, — часть видео не просмотрена.
DETECTOR_BEHIND_HOLD_SEC = float(os.environ.get("CCTV_DETECTOR_BEHIND_HOLD_SEC", "600"))
# Одного кадра в секунду недостаточно для короткого прохода: при 25 fps легко
# выбрать начало и конец силуэта, но не кадр с уверенностью > порога. Два
# кадра/с оставляют несколько подтверждений на проход (человек в кадре
# обычно дольше 2 с) и успевают обработать пятисекундный сегмент быстрее,
# чем появляется следующий. Пять кадров/с (до 25.09.2026) при четырёх
# камерах держали конвейер на ~1.5 ядра круглосуточно: YOLO ел 2–3 кадра/с,
# по 0.5–0.7 ядро-секунды каждый.
RECORDED_PERSON_FPS = float(os.environ.get("CCTV_RECORDED_PERSON_FPS", "2"))
# Сколько кадров могут проходить YOLO одновременно на весь процесс. Модель —
# единственная дорогая часть конвейера, и без общего лимита девять камер в
# ветреную ночь заняли бы все ядра сервера; лимит превращает пик в отставание
# детектора (буфер разбирается медленнее), а не в отказ ботов и записи.
PERSON_INFER_SLOTS = int(os.environ.get("CCTV_PERSON_INFER_SLOTS", "2"))
# Потоки OpenCV на один прогон модели. Замер 25.09.2026 на A1 (Neoverse-N1):
# threads=4 — 172 мс стены, но ~0.6 ядро-секунды; threads=1 — 261 мс и
# 0.26 ядро-секунды. При выборке 2 кадра/с задержка не важна, важна цена.
CV_THREADS = int(os.environ.get("CCTV_CV_THREADS", "1"))
# Детектор ниже приоритетом, чем рекордеры того же процесса и чем боты хоста:
# пропуск записи или задержка ответа человеку хуже, чем тревога на секунду позже.
DETECT_NICE = int(os.environ.get("CCTV_DETECT_NICE", "10"))


def prune_buffer(target: pathlib.Path) -> None:
    files = sorted(target.glob("*.ts"), key=lambda p: p.stat().st_mtime)
    while files and (len(files) > BUFFER_SEGMENTS or sum(p.stat().st_size for p in files) > BUFFER_MAX_BYTES):
        files.pop(0).unlink(missing_ok=True)


HEARTBEAT_INTERVAL_SEC = 10


_PAUSE_CACHE: dict[str, object] = {"at": 0.0, "value": frozenset()}


def paused_cameras(storage: pathlib.Path) -> frozenset:
    """Камеры, снятые с эксплуатации или поставленные на паузу из бота.

    Состояние пишет мост в state/overrides.json; читаем не чаще раза в 5 секунд,
    чтобы файл не открывался на каждом кадре детекции.
    """
    if time.time() - float(_PAUSE_CACHE["at"]) < 5:
        return _PAUSE_CACHE["value"]  # type: ignore[return-value]
    try:
        data = json.loads((settings.engine_state(storage) / "overrides.json").read_text())
        value = frozenset(cam for cam, entry in data.items()
                          if isinstance(entry, dict) and entry.get("status") in ("paused", "retired"))
    except (OSError, ValueError, AttributeError):
        value = frozenset()
    _PAUSE_CACHE["at"], _PAUSE_CACHE["value"] = time.time(), value
    return value


def heartbeat_path(storage: pathlib.Path, camera_id: str) -> pathlib.Path:
    return settings.engine_state(storage) / f"{camera_id}.motion.json"


def write_heartbeat(storage: pathlib.Path, camera_id: str, state: str,
                    reason: str = "", last_motion_at: str | None = None) -> None:
    """Пульс детектора: Bridge — отдельный процесс и иначе о нём ничего не знает.

    Без этого «детекция включена» в теме означала лишь подписку пользователя, а не
    то, что детектор действительно смотрит в камеру.
    """
    target = heartbeat_path(storage, camera_id)
    payload = {"state": state, "reason": reason, "at": now(), "last_motion_at": last_motion_at}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload))
        temp.replace(target)
    except OSError:
        pass  # пульс не обязан ронять детектор


def motion_score(previous, gray) -> float:
    """Доля изменившихся пикселей В ПРОЦЕНТАХ — выделена, чтобы тест проверял шкалу.

    Прежде сравнивалась доля 0..1 с порогом «8», и условие не выполнялось никогда:
    даже полностью разные кадры дают ровно 1.0. Фон камеры шумит на 0.3-1.7 %.
    """
    return float((cv2.absdiff(gray, previous) > 25).mean()) * 100


def to_gray(frame):
    """Уже одноканальный кадр пропускаем: BGR2GRAY на нём бросает исключение."""
    if cv2 is None:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if getattr(frame, "ndim", 2) == 3 else frame


def frames_comparable(previous, gray) -> bool:
    """Сравнивать можно только одинаковую геометрию: RTSP-кадр и ISAPI-снимок
    приходят в разных разрешениях, absdiff на них бросает cv2.error — прежде это
    молча и навсегда убивало поток детектора при первом же переключении источника."""
    return previous is not None and getattr(previous, "shape", None) == getattr(gray, "shape", None)


class Stream:
    """Одно RTSP-соединение детектора на всё время работы, и только substream.

    Прежде VideoCapture открывался под каждый кадр — две с половиной сессии в
    секунду, к тому же в main-профиль (detect_rtsp_url терялся при загрузке
    конфига). Камера не успевала отдавать поток recorder'у (отсюда recorder_failed
    и «Invalid data»), а каждая новая сессия начиналась с кадров без опорного
    I-frame: их распад на макроблоки давал diff, неотличимый от движения.
    """

    def __init__(self, camera: Camera) -> None:
        self.camera, self.capture, self.warmup = camera, None, 0

    def read(self):
        """Кадр и признак «соединение только что открыто»; разрыв виден вызывающему."""
        if cv2 is None: return None, True
        fresh = False
        if self.capture is None:
            self.capture = cv2.VideoCapture(self.camera.detect_rtsp_url or self.camera.rtsp_url)
            self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.warmup, fresh = DETECT_WARMUP, True
        while self.warmup > 0:
            self.warmup -= 1
            if not self.capture.grab(): self.close(); return None, True
        ok, frame = self.capture.read()
        if not ok: self.close(); return None, True
        return frame, fresh

    def close(self) -> None:
        if self.capture is not None: self.capture.release()
        self.capture = None


class RecordedMainStream:
    """Кадры из замкнутого буфера recorder'а, без второго подключения к камере.

    При старте намеренно пропускаем уже лежащие сегменты: перезапуск pipeline не
    должен разослать старые события повторно. Дальше каждый закрытый сегмент
    main-потока читается по порядку. Это также сохраняет нужное разрешение для
    мелкой фигуры на потолочной камере.
    """

    def __init__(self, storage: pathlib.Path, camera: Camera) -> None:
        self.target = settings.engine_buffer(storage) / detect_buffer_name(camera)
        self.camera_id = camera.camera_id
        self.capture = None
        self.current: pathlib.Path | None = None
        self.last_segment: str | None = None
        self.initialized = False
        self.frame_stride = 1
        self.skipped_segments = 0
        self.skipped_at: float | None = None

    def behind(self) -> bool:
        """Детектор недавно терял сегменты: буфер обогнал курсор разбора."""
        return self.skipped_at is not None and time.time() - self.skipped_at < DETECTOR_BEHIND_HOLD_SEC

    @property
    def current_started_at(self) -> float | None:
        """UTC-момент начала текущего сегмента в epoch-секундах.

        Имя сегменту даёт ffmpeg -strftime под TZ=UTC (см. segment_command),
        поэтому оно и есть время съёмки. Без него motion-гейт сравнивал бы
        кадр из прошлого с «сейчас».
        """
        return segment_started_at(self.current)

    @property
    def frame_captured_at(self) -> float | None:
        """UTC-момент съёмки последнего прочитанного кадра в epoch-секундах.

        Детектор разбирает буфер с отставанием (замер 23.09.2026 на dacha2 —
        до 30 с): событие с временем «сейчас» центрировало клип ±15 с мимо
        кадра с человеком. Сегменты пишутся с -reset_timestamps, поэтому
        POS_MSEC — смещение кадра от начала сегмента.
        """
        started = self.current_started_at
        if started is None or self.capture is None:
            return None
        return started + max(0.0, (self.capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000)

    def _segments(self) -> list[pathlib.Path]:
        """Все непустые сегменты камеры по возрастанию имени (= времени съёмки)."""
        try:
            return sorted((path for path in self.target.glob("*.ts") if path.stat().st_size > 0),
                          key=lambda path: path.name)
        except OSError:
            return []

    def _ready_segments(self) -> list[pathlib.Path]:
        """Сегменты, которые ffmpeg уже закрыл: все, кроме самого свежего."""
        # Последний по имени — тот, в который ffmpeg пишет прямо сейчас. Остальные
        # закрыты фактом его появления; выдержка добивает гонку «файл создан,
        # предыдущий ещё дозакрывается».
        cutoff = time.time() - RECORDED_SEGMENT_SETTLE_SEC
        try:
            return [path for path in self._segments()[:-1] if path.stat().st_mtime <= cutoff]
        except OSError:
            return []

    def _open_next(self) -> bool:
        ready = self._ready_segments()
        if not self.initialized:
            # Буфер хранит 10 минут. Его нельзя трактовать как новые события.
            # Базовую отметку ставим по ВСЕМ сегментам, включая открытый: он тоже
            # снят до старта детектора, и после закрытия разбирать его незачем.
            self.initialized = True
            existing = self._segments()
            self.last_segment = existing[-1].name if existing else None
            return False
        pending = [path for path in ready if self.last_segment is None or path.name > self.last_segment]
        if not pending:
            return False
        self._note_skipped(pending[0])
        self.current = pending[0]
        self.capture = cv2.VideoCapture(str(self.current))
        if not self.capture.isOpened():
            self._finish_segment()
            return False
        fps = self.capture.get(cv2.CAP_PROP_FPS) or RECORDED_PERSON_FPS
        self.frame_stride = max(1, round(fps / RECORDED_PERSON_FPS))
        return True

    def _note_skipped(self, following: pathlib.Path) -> None:
        """Журнал тихой потери видео (D-20261003-01).

        Курсор разбора — имя последнего разобранного сегмента. Если самый старый
        сегмент буфера уже новее курсора, буфер удалил промежуточные раньше, чем
        детектор до них дошёл: они не просмотрены. Число — оценка по именам
        (= времени съёмки), разрыв записи у камеры в тот же момент её завышает.
        """
        if self.last_segment is None:
            return
        existing = self._segments()
        if not existing or existing[0].name <= self.last_segment:
            return
        previous = segment_started_at(self.target / self.last_segment)
        started = segment_started_at(following)
        if previous is None or started is None:
            return
        # Сам разобранный сегмент буфер удаляет первым — это ещё не потеря;
        # потеря — когда исчез и следующий за ним.
        count = round((started - previous) / RECORDED_SEGMENT_SEC) - 1
        if count < 1:
            return
        lag = f" lag={time.time() - started:.0f}s"
        self.skipped_segments += count
        self.skipped_at = time.time()
        print(f"detector_skipped_segments camera={self.camera_id} count={count}{lag} "
              f"total={self.skipped_segments}", flush=True)

    def _finish_segment(self) -> None:
        if self.capture is not None:
            self.capture.release()
        if self.current is not None:
            self.last_segment = self.current.name
        self.capture, self.current = None, None

    def read(self):
        """Следующий main-кадр или ``None`` до появления закрытого сегмента."""
        if cv2 is None:
            return None, False
        if self.capture is None and not self._open_next():
            return None, False
        # Берём равномерную выборку, а не первый кадр каждой секунды. ``grab``
        # декодирует без копирования BGR-матрицы и заметно дешевле инференса.
        for _ in range(self.frame_stride - 1):
            if not self.capture.grab():
                self._finish_segment()
                return None, False
        ok, frame = self.capture.read()
        if not ok:
            self._finish_segment()
            return None, False
        if is_smeared(frame):
            # Кадр из рваного начала RTSP-сессии: детектору он бесполезен, а в
            # тревоге выглядит полосами. Пропускаем, следующий I-кадр чистый.
            return None, False
        return frame, False

    def close(self) -> None:
        self._finish_segment()


def is_smeared(frame) -> bool:
    """Кадр, достроенный error concealment после обрезанного I-кадра.

    Декодер тянет последнюю целую строку макроблоков вниз: строки ниже почти
    равны соседям сверху, а поперёк кадра остаются полосы. У честного кадра, даже
    ночного или засвеченного, шум не даёт строкам совпасть; у сплошь ровного
    (закрытый объектив) нет полос — его не трогаем.
    """
    if frame is None or getattr(frame, "ndim", 0) < 2 or frame.shape[0] < 64:
        return False
    import numpy as np
    gray = frame.mean(axis=2) if frame.ndim == 3 else frame
    lower = gray[gray.shape[0] // 8::4, ::4].astype(np.float32)
    rows_equal = np.abs(np.diff(lower, axis=0)).mean(axis=1) < SMEAR_ROW_DIFF
    striped = np.abs(np.diff(lower, axis=1)).mean(axis=1) > SMEAR_COLUMN_DIFF
    return float(np.mean(rows_equal & striped[1:])) >= SMEAR_SHARE


def segment_started_at(path: pathlib.Path | None) -> float | None:
    """`2026-09-04T08:41:03Z.ts` -> epoch. Чужое имя — None, а не исключение."""
    if path is None:
        return None
    try:
        stamp = datetime.datetime.strptime(path.name, "%Y-%m-%dT%H:%M:%SZ.ts")
        return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def onvif_host(camera: Camera) -> str | None:
    """Настоящий адрес камеры — только из snapshot_url.

    В рантайме мост читает НЕ /etc/cctv-bridge/cameras.json, а
    /run/cctv/cameras.proxy.json, где rtsp_url подменён на loopback
    (rtsp://127.0.0.1:18554/...) ради credential-прокси. Гейт, выводивший хост
    из rtsp_url, стучался в 127.0.0.1:80 и получал Connection refused каждые
    10 с. snapshot_url в том же конфиге остаётся прямым адресом камеры.
    """
    for candidate in (camera.snapshot_url, camera.rtsp_url):
        host = urllib.parse.urlparse(candidate).hostname if candidate else None
        if host and not host.startswith("127.") and host != "localhost":
            return host
    return None


def gate_threshold_for(base: float, noise) -> float:
    """Порог гейта = база камеры, поднятая плавающим полом по шуму сцены.

    Пол только поднимает и только до потолка: занизить калиброванную базу
    шумомер не имеет права, а рваный поток не должен закрыть гейт совсем.
    Пока окно не набрано хотя бы на четверть, работает голая база — иначе
    первые же кадры после рестарта задрали бы порог по двум-трём замерам.
    """
    if len(noise) < PERSON_GATE_NOISE_WINDOW // 4:
        return base
    ranked = sorted(noise)
    floor = ranked[min(len(ranked) - 1, int(len(ranked) * PERSON_GATE_NOISE_QUANTILE))] * PERSON_GATE_NOISE_FACTOR
    return max(base, min(floor, PERSON_GATE_NOISE_MAX))


def remember_noise(noise, diff_score, person_found: bool) -> None:
    """Окно шума — только кадры без подтверждённого человека.

    С верхним квартилем (а не медианой) долгий человек в кадре поднял бы пол до
    потолка и на несколько минут ослепил бы гейт для следующего. Поэтому кадр,
    на котором YOLO подтвердил человека, шумом покоя не считается; кадры,
    прошедшие гейт впустую (ветер, свет), — считаются: это и есть шум сцены.
    """
    if diff_score is not None and not person_found:
        noise.append(diff_score)


def build_motion_gate(camera: Camera) -> OnvifMotionGate | None:
    """Гейт строится только когда есть куда стучаться и с чем: ONVIF-стор камеры
    делит те же креды, что и её snapshot (подтверждено на обеих камерах), но
    камера без них не обязана поддерживать пилот людей."""
    if not MOTION_GATE_ENABLED or not camera.snapshot_user or not camera.snapshot_password:
        return None
    host = onvif_host(camera)
    if not host:
        return None
    events_url = f"http://{host}/onvif/Events"
    return OnvifMotionGate(camera.camera_id, events_url, camera.snapshot_user, camera.snapshot_password,
                           hold_seconds=MOTION_GATE_HOLD_SECONDS)


def build_human_gate(camera: Camera) -> OnvifMotionGate | None:
    """Гейт по людям от камеры: только там, где камера умеет их отличать
    (camera_human_events) и есть ONVIF-учётка; иначе — серверный гейт как был."""
    if HUMAN_GATE_MODE == "off" or not camera.camera_human_events:
        return None
    if not camera.snapshot_user or not camera.snapshot_password:
        return None
    host = onvif_host(camera)
    if not host:
        return None
    return OnvifMotionGate(camera.camera_id, f"http://{host}/onvif/Events",
                           camera.snapshot_user, camera.snapshot_password,
                           hold_seconds=HUMAN_GATE_HOLD_SECONDS,
                           topics=("FieldDetector",), label="human_gate")


INFER_SLOTS = threading.BoundedSemaphore(max(1, PERSON_INFER_SLOTS))


def lower_thread_priority(nice: int = DETECT_NICE) -> None:
    """nice только этому потоку: в Linux поток — отдельная задача планировщика,
    поэтому рекордеры того же процесса остаются на обычном приоритете."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except (OSError, AttributeError):
        pass  # без прав на nice детектор всё равно должен работать


def detect(camera: Camera, bridge: Bridge) -> None:
    """RTSP frame-diff primary, two hits; snapshot fallback belongs to Bridge."""
    lower_thread_priority()
    stream, previous, hits = Stream(camera), None, 0
    last_event, last_check, prev_source = 0.0, 0.0, "rtsp"
    window, last_stats = [], time.time()
    storage, last_motion_at, beat_at = bridge.storage, None, 0.0
    # Порог — свойство сцены и потока, а не узла: общий CCTV_MOTION_THRESHOLD (1 %)
    # верен для substream городской камеры (шум p95 0.06 %) и ложно срабатывал бы на
    # даче, где третий поток шумит на p95 0.57 %. Камера может переопределить.
    threshold = camera.motion_threshold if camera.motion_threshold else MOTION_THRESHOLD
    if cv2 is None:
        write_heartbeat(storage, camera.camera_id, "disabled", "opencv_missing")
        return
    person_detector = None
    while camera.person_detection and person_detector is None:
        models = person_models_for(storage)
        generation = models.generation
        try:
            # Детектор камеры — подменяемый: модель меняется из бота на ходу
            # (model_switch), не трогая курсор буфера и очередь событий.
            person_detector = models.register(camera.camera_id)
            model = person_detector.settings
            print(f"person_model camera={camera.camera_id} family={model.family} file={model.model.name} "
                  f"confidence={model.confidence:.2f} reason={model.reason}", flush=True)
        except Exception:
            # Пилот обязан молчать, а не незаметно откатываться к frame-diff:
            # иначе пользователь снова получил бы «движение», а не человека.
            # Ждём исправного выбора модели из бота — он придёт без перезапуска.
            write_heartbeat(storage, camera.camera_id, "disabled", "person_model_unavailable")
            models.wait_change(generation, HEARTBEAT_INTERVAL_SEC)
    calibration = None
    if person_detector is not None:
        # Порог по паре «модель × камера» (threshold_calibration): без команды из
        # бота и без ручного порога остаётся порог модели — как раньше.
        try:
            calibration = person_thresholds_for(storage).camera(camera.camera_id, person_detector)
        except Exception as exc:
            print(f"person_threshold_error camera={camera.camera_id} error={type(exc).__name__}", flush=True)
    recorded_stream = RecordedMainStream(storage, camera) if person_detector is not None else None
    motion_gate = build_motion_gate(camera) if person_detector is not None else None
    if motion_gate is not None:
        motion_gate.start()
    human_gate = build_human_gate(camera) if person_detector is not None else None
    if human_gate is not None:
        human_gate.start()
    gate_scanned, gate_skipped, gate_shadow_skipped = 0, 0, 0
    # Счёт гейта по людям: кадры, на которых камера видела/молчала (обнуляются
    # с каждой статистикой), и события YOLO с подтверждением камеры / без него
    # (накопительно — это и есть материал для вердикта shadow → enforce).
    human_seen, human_blind, human_confirmed, human_unconfirmed = 0, 0, 0, 0
    # Те же кадры, но «по состоянию» камеры (active…inactive ±60 с), а не только
    # по триггерам true в окне: разбор 02.10 показал, что окно −15/+45 с теряет
    # длинные интервалы (p50 47 с, max 264 с). Только телеметрия, решений нет.
    state_seen, state_blind = 0, 0
    keepalive_at, forced_scan_until = 0.0, 0.0
    gate_prev, diff_window = None, []
    # База гейта — из реестра камеры, иначе общая. Плавающий пол считается по
    # своему окну: diff_window обнуляется каждой публикацией статистики и для
    # оценки шума слишком короткоживущий.
    gate_base = camera.person_gate_threshold or PERSON_GATE_THRESHOLD
    gate_threshold = gate_base
    gate_noise: collections.deque = collections.deque(maxlen=PERSON_GATE_NOISE_WINDOW)
    # Фильтр неподвижных объектов — общий для всех камер с YOLO на сервере
    # (решение владельца 26.09.2026: предмет в кадре не должен становиться
    # «человеком»). Считает подавленные срабатывания в person_stats и раз в
    # MOTION_COOLDOWN пишет подробности одного из них.
    still = StillObjectFilter()
    still_suppressed, still_logged_at = 0, 0.0
    hit_scores: list[float] = []  # оценки кадров текущей серии — слабейший из них идёт в калибровку

    def watching(reason: str) -> tuple[str, str]:
        # Пропуск сегментов виден в статусе, а не только в журнале: перегруз иначе незаметен.
        if recorded_stream is not None and recorded_stream.behind():
            return "behind", "detector_skipped_segments"
        return "watching", reason

    while True:
      # Поток обязан пережить любой сбой одной итерации: его смерть — слепой детектор
      # до рестарта юнита, а наружу об этом говорил только протухший пульс.
      try:
        source = "rtsp"
        if recorded_stream is not None:
            source = "recorded_main_buffer"
            frame, fresh = recorded_stream.read()
            if frame is None:
                if time.time() - beat_at >= HEARTBEAT_INTERVAL_SEC:
                    write_heartbeat(storage, camera.camera_id, *watching("waiting_for_recorded_main"), last_motion_at)
                    beat_at = time.time()
                time.sleep(0.25)
                continue
        else:
            frame, fresh = stream.read()
        if frame is None:
            # Approved fallback: compare successive ISAPI snapshots when RTSP cannot be read.
            source = "snapshot"
            try:
                jpeg, _ = bridge.snapshot(camera)
                frame = cv2.imdecode(__import__("numpy").frombuffer(jpeg, dtype="uint8"), cv2.IMREAD_COLOR)
            except Exception:
                frame = None
            if frame is None:
                write_heartbeat(storage, camera.camera_id, "blind", "no_frames", last_motion_at)
                beat_at = time.time(); previous, hits = None, 0; time.sleep(3); continue
            # Смену источника ловит prev_source; сами по себе последовательные
            # снимки сравнимы, поэтому fresh здесь не взводится.
            fresh = False
            time.sleep(DETECT_INTERVAL)
        if source == "rtsp" and time.time() - last_check < DETECT_INTERVAL:
            continue  # кадр вычитан и учтён как «прочитан», замеряем не каждый
        last_check = time.time()
        if person_detector is not None:
            gate_closed, keepalive = False, False
            # Основной гейт: изменилась ли картинка по сравнению с прошлым
            # main-кадром. Дёшево, не зависит от камеры и уже доказано в бою.
            gate_gray = to_gray(frame)
            diff_score = motion_score(gate_prev, gate_gray) if frames_comparable(gate_prev, gate_gray) else None
            gate_prev = gate_gray
            # Память фильтра неподвижных объектов пополняется КАЖДЫМ кадром, в
            # том числе пропущенным гейтом, по времени съёмки кадра: буфер
            # разбирается с отставанием и рывками, «сейчас» тут не годится.
            shot_at = recorded_stream.frame_captured_at if recorded_stream is not None else None
            frame_at = shot_at if shot_at is not None else time.time()
            still.remember(frame_at, gate_gray)
            if diff_score is not None:
                diff_window.append(diff_score)
                gate_threshold = gate_threshold_for(gate_base, gate_noise)
                gate_closed = diff_score < gate_threshold
            if gate_closed and motion_gate is not None:
                # ONVIF может гейт только открыть: его VMD не доказан, поэтому
                # доверять ему закрытие нельзя. Кадр из буфера снят в прошлом,
                # значит спрашиваем про время ЕГО съёмки, а не про «сейчас».
                started_at = recorded_stream.current_started_at if recorded_stream is not None else None
                if started_at is not None:
                    if motion_gate.active_between(started_at - MOTION_GATE_PRE_SEC,
                                                  started_at + MOTION_GATE_POST_SEC):
                        gate_closed = False
                elif motion_gate.is_open():
                    gate_closed = False
            # Гейт по людям от камеры — независимый от frame-diff вопрос: «видела
            # ли камера человека в момент съёмки этого кадра». Нездоровая
            # подписка отвечает «да» (fail-open) — как и ONVIF-гейт выше.
            # camera_saw — ответ камеры как есть; ниже forced_scan и keepalive
            # переоткрывают human_closed, но НЕ его. 26.09.2026: метка
            # camera_human бралась из human_closed уже после переоткрытия, и
            # шесть ложных YOLO-тревог dacha3 (мешки в углу, 0.35–0.41) ушли
            # в журнал как «камера подтвердила» при events=0 за весь день.
            human_closed, camera_saw, camera_state = False, None, None
            if human_gate is not None:
                shot = recorded_stream.frame_captured_at if recorded_stream is not None else None
                if shot is None and recorded_stream is not None:
                    shot = recorded_stream.current_started_at
                if shot is not None:
                    camera_saw = human_gate.active_between(shot - HUMAN_GATE_PRE_SEC, shot + HUMAN_GATE_POST_SEC)
                else:
                    camera_saw = human_gate.is_open()
                if shot is not None:
                    camera_state = human_gate.state_active_near(shot, HUMAN_GATE_STATE_MARGIN_SEC)
                    if camera_state:
                        state_seen += 1
                    else:
                        state_blind += 1
                human_closed = not camera_saw
                if camera_saw:
                    human_seen += 1
                else:
                    human_blind += 1
            if (gate_closed or human_closed) and time.time() < forced_scan_until:
                gate_closed = human_closed = False
            if (gate_closed or human_closed) and time.time() - keepalive_at >= MOTION_GATE_KEEPALIVE_SEC:
                # Предохранитель: любой гейт может ошибаться систематически.
                # Раз в KEEPALIVE смотрим кадр вопреки ему — иначе отказ гейта
                # означал бы тихую смерть детекции вместо шумной.
                gate_closed, human_closed, keepalive = False, False, True
                keepalive_at = time.time()
            skip_reason = None
            if gate_closed and PERSON_GATE_MODE == "enforce":
                skip_reason = "motion_gate_closed"
            elif human_closed and HUMAN_GATE_MODE == "enforce":
                skip_reason = "human_gate_closed"
            if skip_reason is not None:
                # Кадр всё равно декодирован (курсор буфера должен идти вперёд,
                # иначе после открытия гейта пришлось бы разбирать очередь
                # старых сегментов), но самый дорогой шаг — YOLO — пропущен.
                # hits сбрасываем: «два подряд» не должно склеивать сигналы
                # из разных, разорванных гейтом, окон.
                hits, hit_scores = 0, []
                gate_skipped += 1
                remember_noise(gate_noise, diff_score, False)
                if time.time() - beat_at >= HEARTBEAT_INTERVAL_SEC:
                    write_heartbeat(storage, camera.camera_id, *watching(skip_reason), last_motion_at)
                    beat_at = time.time()
                time.sleep(0 if recorded_stream is not None else PERSON_DETECT_INTERVAL)
                continue
            if gate_closed:
                gate_shadow_skipped += 1  # shadow: посчитали, но кадр всё же смотрим
            else:
                gate_scanned += 1
            # Не чаще раза в секунду: измеренный на сервере прогон занимает ~80 мс
            # на кадр, а частота камеры тут не повышает качество тревоги.
            try:
                with INFER_SLOTS:
                    found, score, box = person_detector.detect(frame)
            except Exception:
                write_heartbeat(storage, camera.camera_id, "blind", "person_detector_error", last_motion_at)
                time.sleep(3); continue
            still_note, still_object = "", False
            if found:
                # Сеть увидела «человека» — но двигается ли он? Предмет (мешки,
                # ведро, ночной блик) стоит на месте, и его рамка не отличается
                # от того же места кадра несколько секунд назад. Подавленное
                # срабатывание — шум сцены, а не человек: ниже оно идёт и в
                # окно шума гейта, и в счётчик still.
                verdict = still.judge(frame_at, box, score)
                still_note = " " + verdict.note()
                if not verdict.moving:
                    found, still_object = False, True
                    still_suppressed += 1
                    if time.time() - still_logged_at >= MOTION_COOLDOWN:
                        still_logged_at = time.time()
                        print(f"person_still camera={camera.camera_id} confidence={score:.2f} "
                              f"box={','.join(f'{v:.3f}' for v in box)}{still_note}", flush=True)
            hits = hits + 1 if found else 0
            hit_scores = (hit_scores + [score])[-PERSON_HITS:] if found else []
            remember_noise(gate_noise, diff_score, found)
            if calibration is not None:
                # Нездоровая подписка камеры отвечает «видела» на всё (fail-open) —
                # для калибровки это «не знаю», а не человек на каждом кадре.
                healthy_camera = human_gate is not None and human_gate.healthy
                calibration.observe(frame_at, score, found, still_object,
                                    bool(camera_saw) if healthy_camera else None)
            window.append(score)
            if found and human_closed:
                # Камера человека не заметила, а YOLO видит: в enforce такой кадр
                # приходит только по keepalive, и окно принудительного скана —
                # единственный способ дособрать «два подряд» для события.
                forced_scan_until = time.time() + MOTION_GATE_HOLD_SECONDS
            if found and (keepalive or gate_closed):
                # Человек найден на кадре, который гейт считал ненужным. В
                # enforce это единственный шанс заметить мёртвый VMD, поэтому
                # открываем окно принудительного скана; в shadow — громкая
                # запись в журнал: она и есть цена ошибки гейта.
                forced_scan_until = time.time() + MOTION_GATE_HOLD_SECONDS
                print(f"gate_missed_person camera={camera.camera_id} confidence={score:.2f} "
                      f"mode={PERSON_GATE_MODE} keepalive={int(keepalive)} "
                      f"diff={diff_score if diff_score is None else round(diff_score, 3)}", flush=True)
            if hits >= PERSON_HITS and time.time() - last_event >= MOTION_COOLDOWN:
                # Время события — момент съёмки кадра, а не «сейчас»: по нему
                # бридж центрирует клип (см. RecordedMainStream.frame_captured_at).
                shot_at = recorded_stream.frame_captured_at if recorded_stream is not None else None
                captured_at = (datetime.datetime.fromtimestamp(shot_at, datetime.timezone.utc)
                               .replace(microsecond=0).isoformat() if shot_at is not None else now())
                lag = f" lag={time.time() - shot_at:.0f}s" if shot_at is not None else ""
                seen = ""
                if human_gate is not None:
                    # Материал вердикта: подтверждённый проход — camera_human=1;
                    # camera_human=0 — либо ложное YOLO (ИК-шум, рассвет), либо
                    # пропуск камеры, и это решается по снимку глазами.
                    # Источник — camera_saw, а не human_closed: последний после
                    # keepalive/forced_scan всегда False (см. выше).
                    seen = f" camera_human={int(bool(camera_saw))}"
                    if camera_state is not None:
                        seen += f" camera_state={int(camera_state)}"
                    if not camera_saw:
                        human_unconfirmed += 1
                        print(f"human_gate_unconfirmed camera={camera.camera_id} confidence={score:.2f} "
                              f"mode={HUMAN_GATE_MODE} events={human_gate.motion_count}", flush=True)
                    else:
                        human_confirmed += 1
                if calibration is not None:
                    calibration.event(min(hit_scores[-PERSON_HITS:] or [score]),
                                      bool(camera_saw) if human_gate is not None else False)
                print(f"person camera={camera.camera_id} confidence={score:.2f}{lag}{seen}"
                      f" box={','.join(f'{v:.3f}' for v in box)}{still_note}", flush=True)
                # Не берём новый snapshot по RTSP/ISAPI: это была бы ещё одна
                # сессия к камере. В уведомление идёт ровно main-кадр, на котором
                # YOLO подтвердил человека.
                ok, encoded = cv2.imencode(".jpg", frame)
                # Пауза глушит только исходящее событие: детекция продолжает
                # идти, и по кнопке кадр всё ещё доступен.
                if ok and camera.camera_id not in paused_cameras(storage):
                    bridge.motion(camera, captured_at, source="recorded_main_person_detector",
                                  snapshot_body=encoded.tobytes())
                last_motion_at = captured_at; last_event = time.time(); hits = 0; hit_scores = []
            if window and time.time() - last_stats >= STATS_INTERVAL:
                # Без этого лога порог CCTV_PERSON_CONFIDENCE калибруется вслепую:
                # ни одного срабатывания за часы не отличить от «камере некого
                # снимать» и от «модель всегда занижает уверенность».
                ranked = sorted(window)
                pick = lambda q: ranked[min(len(ranked) - 1, int(len(ranked) * q))]
                print(f"person_stats camera={camera.camera_id} n={len(ranked)} "
                      f"p50={pick(0.5):.2f} p95={pick(0.95):.2f} max={ranked[-1]:.2f} "
                      f"confidence={person_detector.confidence:.2f} still={still_suppressed}", flush=True)
                still_suppressed = 0
                # Доля пропущенного — обещанная экономия CPU; diff-персентили
                # нужны, чтобы порог гейта ставился по замеру, а не на глаз.
                total = gate_scanned + gate_skipped + gate_shadow_skipped
                saved = (gate_skipped + gate_shadow_skipped) / total * 100 if total else 0.0
                diffs = sorted(diff_window)
                take = lambda q: diffs[min(len(diffs) - 1, int(len(diffs) * q))] if diffs else 0.0
                onvif = f" onvif_motion={motion_gate.motion_count}" if motion_gate is not None else ""
                print(f"gate_stats camera={camera.camera_id} mode={PERSON_GATE_MODE} "
                      f"threshold={gate_threshold:.2f}% base={gate_base:.2f}% scanned={gate_scanned} "
                      f"skipped={gate_skipped} shadow_skipped={gate_shadow_skipped} saved={saved:.0f}% "
                      f"diff_p50={take(0.5):.3f}% diff_p95={take(0.95):.3f}% "
                      f"diff_max={diffs[-1] if diffs else 0.0:.3f}%{onvif}", flush=True)
                if human_gate is not None:
                    # saved — доля кадров, которые enforce не пустил бы в YOLO;
                    # events — сколько раз камера вообще подавала сигнал (0 при
                    # живой подписке сутки подряд = FieldDetection не армирован).
                    frames = human_seen + human_blind
                    print(f"human_gate_stats camera={camera.camera_id} mode={HUMAN_GATE_MODE} "
                          f"healthy={int(human_gate.healthy)} events={human_gate.motion_count} "
                          f"seen={human_seen} blind={human_blind} "
                          f"saved={human_blind / frames * 100 if frames else 0.0:.0f}% "
                          f"confirmed={human_confirmed} unconfirmed={human_unconfirmed} "
                          f"state_seen={state_seen} state_blind={state_blind}", flush=True)
                    human_seen, human_blind, state_seen, state_blind = 0, 0, 0, 0
                gate_scanned, gate_skipped, gate_shadow_skipped = 0, 0, 0
                diff_window = []
                window, last_stats = [], time.time()
            if time.time() - beat_at >= HEARTBEAT_INTERVAL_SEC:
                write_heartbeat(storage, camera.camera_id, *watching("person_detector"), last_motion_at)
                beat_at = time.time()
            # Файловый main-поток не держит камеру: его можно читать настолько
            # быстро, насколько успевает модель. RTSP/substream сохраняет
            # прежнюю щадящую частоту.
            time.sleep(0 if recorded_stream is not None else PERSON_DETECT_INTERVAL)
            continue
        gray = to_gray(frame)
        if fresh or source != prev_source:
            # После разрыва предыдущий кадр устарел на неизвестный срок, а кадр
            # другого источника — другой геометрии: diff дал бы ложное движение.
            previous, hits = None, 0
        if frames_comparable(previous, gray):
            score = motion_score(previous, gray)
            window.append(score)
            hits = hits + 1 if score >= threshold else 0
            if hits >= 2 and time.time() - last_event >= MOTION_COOLDOWN:
                print(f"motion camera={camera.camera_id} score={score:.2f}%", flush=True)
                if camera.camera_id not in paused_cameras(storage):
                    bridge.motion(camera, now())
                last_motion_at = now(); last_event = time.time(); hits = 0
        if window and time.time() - last_stats >= STATS_INTERVAL:
            ranked = sorted(window)
            pick = lambda q: ranked[min(len(ranked) - 1, int(len(ranked) * q))]
            print(f"motion_stats camera={camera.camera_id} n={len(ranked)} "
                  f"p50={pick(0.5):.2f}% p95={pick(0.95):.2f}% max={ranked[-1]:.2f}% "
                  f"threshold={threshold:.2f}%", flush=True)
            window, last_stats = [], time.time()
        if time.time() - beat_at >= HEARTBEAT_INTERVAL_SEC:
            write_heartbeat(storage, camera.camera_id, "watching" if source == "rtsp" else "degraded",
                            "" if source == "rtsp" else "rtsp_unreadable", last_motion_at)
            beat_at = time.time()
        previous, prev_source = gray, source
      except Exception:
        write_heartbeat(storage, camera.camera_id, "blind", "detector_error", last_motion_at)
        beat_at = time.time(); previous, hits = None, 0; time.sleep(3)


PERSON_MODELS: ModelManager | None = None
_PERSON_MODELS_LOCK = threading.Lock()


def person_models_for(storage: pathlib.Path) -> ModelManager:
    """Менеджер моделей процесса: один на все камеры (смена модели — сразу для всех)."""
    global PERSON_MODELS
    with _PERSON_MODELS_LOCK:
        if PERSON_MODELS is None:
            PERSON_MODELS = ModelManager(settings.engine_state(storage))
            PERSON_MODELS.start()
        return PERSON_MODELS


PERSON_THRESHOLDS: ThresholdManager | None = None


def person_thresholds_for(storage: pathlib.Path) -> ThresholdManager:
    """Пороги камер процесса: автокалибровка при смене модели и по заявке из бота."""
    global PERSON_THRESHOLDS
    models = person_models_for(storage)
    with _PERSON_MODELS_LOCK:
        if PERSON_THRESHOLDS is None:
            PERSON_THRESHOLDS = ThresholdManager(settings.engine_state(storage), models)
            PERSON_THRESHOLDS.start()
        return PERSON_THRESHOLDS


def main() -> None:
    config = json.loads(pathlib.Path(os.environ.get("CCTV_RUNTIME_CAMERA_CONFIG") or os.environ.get("CCTV_CAMERA_CONFIG") or str(settings.config_dir() / settings.CAMERAS_FILE)).read_text())
    storage = pathlib.Path(os.environ.get("CCTV_STORAGE_ROOT", settings.DEFAULT_STORAGE_ROOT))
    bridge = Bridge(config, storage, os.environ.get("CCTV_PUBLIC_URL") or f"http://127.0.0.1:{settings.DEFAULT_BRIDGE_PORT}")
    check_events_url(bridge)
    if cv2 is not None:
        cv2.setNumThreads(CV_THREADS)
        # Заявки на смену модели из бота принимаются и без камер с YOLO:
        # выбор проверяется и запоминается до того, как детекция включена.
        person_thresholds_for(storage)
    for camera in bridge.cameras.values():
        threading.Thread(target=record, args=(camera, storage), daemon=True).start()
        if camera.person_detection and detect_buffer_name(camera) != camera.camera_id:
            threading.Thread(target=record, args=(camera, storage, camera.detect_rtsp_url,
                                                  detect_buffer_name(camera)), daemon=True).start()
        threading.Thread(target=detect, args=(camera, bridge), daemon=True).start()
    while True: time.sleep(3600)


if __name__ == "__main__": main()
