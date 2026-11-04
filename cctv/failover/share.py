"""Один конфиг на всех: активный узел раздаёт свой конфиг вооружённым.

Раздаются две вещи — обе по каналу агентов слоя (mTLS установки + подпись
общим секретом, wire.py), без участия движка и бота:

* **каталог конфига** — config.toml, secrets.toml, cameras.json (FILES). Копию
  каждый агент держит в своём state (SHARED_DIR) с ревизией (rev, origin, sha)
  и запускает роль на ней, а не на локальном каталоге узла. Новую ревизию
  выпускает узел, который держит роль: человек правит каталог конфига там, и
  правка за секунды уходит остальным. Правка на узле наготове не раздаётся
  (журнал об этом пишет), иначе устаревший каталог запасного узла после
  переключения перетёр бы настоящий.
* **настройки роли из чата** — файлы state движка, которые правит бот через
  мост: реестр камер, пауза/вывод камер, пароли активированных камер, выбор
  модели, ручной порог (ROLE_FILES).
  Держатель роли рассылает их агентам той же роли; узел наготове пишет их к
  себе только пока роль у него не запущена.

Узел с разделом [failover] берёт настройки из раздаваемого конфига даже если
в его окружении (.env) есть устаревшее значение: окружение узла сильнее только
для узловых ключей (NODE_LOCAL — имя, пути, порты, размеры буфера, свои
сертификаты, своя сеть). Новая настройка, не попавшая ни в раздаваемые файлы,
ни в этот список, валит тест tests/test_failover_config.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
from typing import Mapping

from .. import settings

FILES = (settings.CONFIG_FILE, settings.SECRETS_FILE, settings.CAMERAS_FILE)
SHARED_DIR = "failover-config"
META_FILE = "meta.json"
SEEN_FILE = "local.json"
MAX_BYTES = 1024 * 1024

# Настройки роли в её state, которые правят из чата: реестр камер, пауза и вывод камер,
# пароли активированных камер, выбор модели, ручной порог. Раздаются держателем роли.
ROLE_FILES = {
    "engine": ("cameras.json", "overrides.json", "activation-vault.json", "person_model.json",
               "person_threshold.override.json"),
    "bot": (),
}
# Остальное в state роли — не настройки: переезжает с ролью при переключении (handover)
# или остаётся узлу. Новое имя файла state обязано попасть сюда или в ROLE_FILES (тест).
ROLE_KEPT = {
    # калибровка порога по сцене, заявки и ход переключений, пульс и память кадра камер,
    # копии реестра до правки, суточная диагностика
    "engine": ("person_threshold.json", "person_model.request.json", "person_model.status.json",
               "person_threshold.request.json", "{camera}.motion.json", "{camera}.still.json",
               "registry-backups", "diag"),
    # база бота: владелец, группа, темы, люди — данные роли, переезжают при переключении
    "bot": ("bot.sqlite3",),
}

# Узловые ключи: окружение узла для них сильнее раздаваемого конфига.
NODE_LOCAL = frozenset({
    # имя узла и пути внутри контейнера
    "CCTV_NODE", "CCTV_CONFIG_DIR", "CCTV_STATE_DIR", "CCTV_BUFFER_DIR", "CCTV_STORAGE_ROOT",
    "CCTV_RUNTIME_DIR", "CCTV_SUPERVISOR_STATUS", "CCTV_CAMERA_CONFIG", "CCTV_PROXY_CAMERA_CONFIG",
    "CCTV_RUNTIME_CAMERA_CONFIG", "CCTV_REGISTRY_FILE", "CCTV_REGISTRY_SEED", "CCTV_PROVISION_SOCKET",
    "CCTV_PROVISION_BACKUPS", "CCTV_PROVISION_UNITS", "CCTV_RETENTION_GUARD", "CCTV_MODEL_DIRS",
    "CCTV_NOTIFY_STATE",
    # адреса и порты этой машины, связь со своей соседней ролью
    "CCTV_BIND", "CCTV_PORT", "CCTV_BRIDGE_PORT", "CCTV_PUBLIC_URL", "CCTV_EVENTS_HOST", "CCTV_EVENTS_PORT",
    "CCTV_EVENTS_URL", "CCTV_BRIDGE_URL", "CCTV_RTSP_PROXY_PORT", "CCTV_NODES_URL", "CCTV_NODES_TOKEN",
    "CCTV_POLL_CONFLICT", "CCTV_ORIGIN",
    # свои сертификаты связи движок↔бот и пользователь писаря
    "CCTV_BRIDGE_CA", "CCTV_BRIDGE_CLIENT_CERT", "CCTV_BRIDGE_CLIENT_KEY", "CCTV_CLIENT_CA",
    "CCTV_SERVER_CERT", "CCTV_SERVER_KEY", "CCTV_EVENTS_CA", "CCTV_EVENTS_CERT", "CCTV_EVENTS_CLIENT_CA",
    "CCTV_EVENTS_KEY", "CCTV_BRIDGE_USER", "CCTV_BRIDGE_GROUP",
    # сеть этой машины и её Bot API-сервер
    "CCTV_DISCOVERY_NETWORKS", "CCTV_TELEGRAM_API",
    # память, диск и процессор этой машины
    "CCTV_BUFFER_MAX_BYTES", "CCTV_BUFFER_SEGMENTS", "CCTV_MAX_TOTAL_BYTES", "CCTV_STORAGE_BUDGET_BYTES",
    "CCTV_MIN_FREE_BYTES", "CCTV_CV_THREADS", "CCTV_DETECT_NICE", "CCTV_PERSON_INFER_SLOTS",
})


class ShareError(RuntimeError):
    """Присланный конфиг не годится этому узлу — не принят."""


def digest(files: Mapping[str, str | None]) -> str:
    return hashlib.sha256(json.dumps(dict(sorted(files.items())), ensure_ascii=False).encode()).hexdigest()


def read_files(root: pathlib.Path, names) -> dict[str, str | None]:
    """Содержимое файлов (None — файла нет); каталоги и прочее — тоже None."""
    result = {}
    for name in names:
        try:
            result[name] = (root / name).read_text()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            result[name] = None
    return result


def write_files(root: pathlib.Path, files: Mapping[str, str | None]) -> None:
    """Атомарно по файлу: tmp + replace, режим 0600; None — удалить."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name
        if text is None:
            path.unlink(missing_ok=True)
            continue
        fd, tmp = tempfile.mkstemp(dir=root, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            os.chmod(tmp, 0o600)   # пароли камер и токены — только пользователю контейнера
            os.replace(tmp, path)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise


def _json(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def has_layer(config_dir: pathlib.Path) -> bool:
    """Локальный каталог узла включает слой (раздел [failover]) — без него копии нет."""
    try:
        return bool(settings._read_toml(config_dir / settings.CONFIG_FILE).get("failover"))
    except settings.SettingsError:
        return False


class Store:
    """Копия раздаваемого каталога конфига в state агента."""

    def __init__(self, state_dir: pathlib.Path) -> None:
        self.root = pathlib.Path(state_dir) / SHARED_DIR

    def meta(self) -> dict:
        meta = _json(self.root / META_FILE)
        return {"rev": int(meta.get("rev") or 0), "origin": str(meta.get("origin") or ""),
                "sha": str(meta.get("sha") or "")}

    def files(self) -> dict[str, str | None]:
        return read_files(self.root, FILES)

    def bundle(self) -> dict:
        return {**self.meta(), "files": self.files()}

    def save(self, rev: int, origin: str, files: Mapping[str, str | None]) -> None:
        """Файлы, затем meta.json: супервизор видит новую ревизию по meta."""
        write_files(self.root, {name: files.get(name) for name in FILES})
        write_files(self.root, {META_FILE: json.dumps({"rev": rev, "origin": origin, "sha": digest(files)})})

    def seen(self) -> str | None:
        """sha локального каталога, который агент уже видел (None — ни разу)."""
        value = _json(self.root / SEEN_FILE).get("sha")
        return value if isinstance(value, str) else None

    def mark_seen(self, sha: str) -> None:
        write_files(self.root, {SEEN_FILE: json.dumps({"sha": sha})})


def check_bundle(bundle: Mapping, me: str, role: str) -> dict[str, str | None]:
    """Файлы присланной ревизии, если на них этот узел запустится; иначе ShareError.

    Конфиг, в котором узла нет или раздел [failover] сломан, не принимается:
    иначе узел потерял бы связь с соседями и уже не получил бы исправление.
    """
    from . import config as fo_config

    files = bundle.get("files")
    if not isinstance(files, dict) or set(files) != set(FILES):
        raise ShareError("files")
    if any(text is not None and not isinstance(text, str) for text in files.values()):
        raise ShareError("files")
    if sum(len(text or "") for text in files.values()) > MAX_BYTES:
        raise ShareError("too_big")
    if digest(files) != bundle.get("sha"):
        raise ShareError("sha")
    with tempfile.TemporaryDirectory() as tmp:
        write_files(pathlib.Path(tmp), files)
        env = {"CCTV_CONFIG_DIR": tmp, "CCTV_NODE": me}
        try:
            settings.load(role, env)
            if fo_config.load(env) is None:
                raise ShareError("no_failover")
            if files.get(settings.CAMERAS_FILE) is not None:
                cameras = json.loads(files[settings.CAMERAS_FILE]).get("cameras", [])
                if not isinstance(cameras, list):
                    raise ShareError("cameras")
        except (settings.SettingsError, fo_config.FailoverError, ValueError, AttributeError) as exc:
            raise ShareError(f"{type(exc).__name__}: {exc}") from exc
    return dict(files)


def newer(item: Mapping, current: Mapping, rank) -> bool:
    """Ревизия item новее current: больше rev; при равной и другом содержимом — главнее узел."""
    if int(item.get("rev") or 0) != int(current.get("rev") or 0):
        return int(item.get("rev") or 0) > int(current.get("rev") or 0)
    if item.get("sha") == current.get("sha"):
        return False
    return rank(str(item.get("origin") or "")) < rank(str(current.get("origin") or ""))


def effective_env(role: str, env: Mapping[str, str]) -> dict[str, str]:
    """Окружение роли с раздаваемым конфигом вместо локального каталога узла.

    Копии нет (или в локальном каталоге нет [failover]) — окружение как есть,
    одноузловая установка не меняется. Копия есть — CCTV_CONFIG_DIR указывает
    на неё, а значения узлового окружения, которые конфиг задаёт сам, снимаются
    (кроме NODE_LOCAL): раздаваемый конфиг сильнее устаревшего .env узла.
    """
    result = dict(env)
    if not has_layer(settings.config_dir(env)):
        return result
    store = Store(settings.state_dir(env))
    if not store.meta()["rev"]:
        return result
    result["CCTV_CONFIG_DIR"] = str(store.root)
    try:
        values = settings.load(role, result)
    except settings.SettingsError:
        return dict(env)
    for key in values:
        if key not in NODE_LOCAL:
            result.pop(key, None)
    return result


def watched(env: Mapping[str, str]) -> tuple[pathlib.Path, ...]:
    """Файл, по которому супервизор замечает новую ревизию копии."""
    return (Store(settings.state_dir(env)).root / META_FILE,)


def role_state(state_dir: pathlib.Path, role: str) -> dict:
    files = read_files(pathlib.Path(state_dir), ROLE_FILES.get(role, ()))
    return {"sha": digest(files), "files": files}


def save_role_state(state_dir: pathlib.Path, role: str, files: Mapping) -> None:
    names = ROLE_FILES.get(role, ())
    if not isinstance(files, Mapping) or set(files) != set(names):
        raise ShareError("files")
    if any(text is not None and not isinstance(text, str) for text in files.values()):
        raise ShareError("files")
    if sum(len(text or "") for text in files.values()) > MAX_BYTES:
        raise ShareError("too_big")
    for name, text in files.items():
        if text is not None and name.endswith(".json"):
            try:
                json.loads(text)
            except ValueError as exc:
                raise ShareError(f"{name}: {exc}") from exc
    write_files(pathlib.Path(state_dir), dict(files))
