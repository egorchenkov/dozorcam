# rknn-toolkit2 для scripts/models/rknn-convert.sh: только x86_64 — сборка на arm64
# (колесо aarch64) ищет NPU и падает на build. Версии закреплены: рантайм librknnrt
# на устройстве должен быть той же версии, что toolkit (docs/models.md).
FROM --platform=linux/amd64 python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
      torch==2.2.0+cpu rknn-toolkit2==2.5.0 "setuptools<81"
WORKDIR /src
