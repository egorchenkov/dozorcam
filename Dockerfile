# syntax=docker/dockerfile:1.7
# Образ Dozorcam: движок (мост, конвейер, RTSP-прокси) и бот — одна сборка, роль задаёт команда.
#
#   docker buildx build --platform linux/arm64,linux/amd64 -t dozorcam:dev .
#
# Сборка кросс-платформенная без эмуляции: всё, что исполняется, работает в стадиях
# под $BUILDPLATFORM (pip ставит колёса целевой архитектуры через --platform), а
# финальная стадия под $TARGETPLATFORM состоит только из COPY — QEMU/binfmt на
# сборочном хосте не нужен. Поэтому же в финальной стадии нет RUN: пользователь
# задан числом, каталоги приходят готовыми из сборочной стадии.

ARG PYTHON_IMAGE=python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
# Статические ffmpeg/ffprobe (musl, без зависимостей) под обе архитектуры. У прода — 6.1.1 из Ubuntu.
ARG FFMPEG_IMAGE=mwader/static-ffmpeg:7.1.1@sha256:11a44711684c0b9f754c047dcd64235b8b52deab251bd0e0a86f22faa160749c

FROM ${FFMPEG_IMAGE} AS ffmpeg

FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS build
ARG TARGETARCH
# В образе — только модель с разрешительной лицензией: YOLOX-Tiny из релиза Megvii
# 0.1.1rc0 (Apache-2.0), модель по умолчанию по бенчу bench/REPORT.md. Веса YOLOv5/YOLOv8
# (Ultralytics, AGPL-3.0) пользователь кладёт сам в config/models/ — docs/models.md.
ARG MODEL_URL=https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx
ARG MODEL_SHA256=427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7
ADD --checksum=sha256:${MODEL_SHA256} ${MODEL_URL} /out/usr/share/cctv/models/yolox_tiny.onnx
# ADD с URL кладёт файл 0600 root — chmod ниже, иначе non-root детектор его не прочтёт.
COPY requirements.txt /src/requirements.txt
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64 ;; \
      arm64) arch=aarch64 ;; \
      *) echo "архитектура $TARGETARCH не поддерживается" >&2; exit 1 ;; \
    esac; \
    pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        --target /out/site-packages --only-binary=:all: \
        --implementation cp --python-version 3.12 \
        --platform manylinux_2_28_$arch --platform manylinux_2_17_$arch --platform manylinux2014_$arch \
        -r /src/requirements.txt
COPY pyproject.toml README.md /src/
COPY cctv /src/cctv
# Пакет чистый Python: собираем колесо и раскладываем без зависимостей (они уже выше).
RUN pip wheel --no-cache-dir --disable-pip-version-check --no-deps -w /tmp/wheel /src \
 && pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        --no-deps --no-compile --target /out/site-packages /tmp/wheel/*.whl \
 && rm -rf /out/site-packages/bin
COPY scripts/retention-guard.sh /out/app/scripts/retention-guard.sh
# Лицензия и перечень сторонних компонентов (ffmpeg GPL, модель, пакеты) — внутри образа.
COPY LICENSE NOTICE THIRD_PARTY.md /out/usr/share/doc/dozorcam/
# Точки монтирования: state — том, buffer/spool и /run/cctv — tmpfs (compose.yml),
# buffer-disk — том буфера на диске (владелец переходит в пустой том при первом монтировании).
RUN install -d -m 0700 /out/var/lib/cctv/state /out/var/lib/cctv/buffer /out/var/lib/cctv/spool \
        /out/var/lib/cctv/buffer-disk /out/run/cctv \
 && install -d -m 0755 /out/etc/cctv \
 && chmod 0755 /out/app/scripts/retention-guard.sh \
 && chmod 0644 /out/usr/share/cctv/models/yolox_tiny.onnx

FROM ${PYTHON_IMAGE}
ARG CCTV_UID=10001
LABEL org.opencontainers.image.title="Dozorcam" \
      org.opencontainers.image.source="https://github.com/egorchenkov/dozorcam" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="Self-hosted camera watcher: RTSP buffer, person detection, Telegram forum bot"
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=build /out/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /out/usr/share/cctv /usr/share/cctv
COPY --from=build /out/usr/share/doc/dozorcam /usr/share/doc/dozorcam
COPY --from=build /out/app /app
COPY --from=build --chown=${CCTV_UID}:${CCTV_UID} /out/var/lib/cctv /var/lib/cctv
COPY --from=build --chown=${CCTV_UID}:${CCTV_UID} /out/run/cctv /run/cctv
COPY --from=build /out/etc/cctv /etc/cctv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CCTV_CONFIG_DIR=/etc/cctv \
    CCTV_STATE_DIR=/var/lib/cctv/state \
    CCTV_BUFFER_DIR=/var/lib/cctv/buffer \
    CCTV_STORAGE_ROOT=/var/lib/cctv/spool \
    CCTV_RUNTIME_DIR=/run/cctv \
    CCTV_SUPERVISOR_STATUS=/run/cctv/supervisor.json \
    CCTV_PROVISION_SOCKET=/run/cctv/provision.sock \
    CCTV_RETENTION_GUARD=/app/scripts/retention-guard.sh
USER ${CCTV_UID}:${CCTV_UID}
WORKDIR /var/lib/cctv/state
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "cctv.container", "health"]
ENTRYPOINT ["python", "-m", "cctv.container"]
CMD ["engine"]
