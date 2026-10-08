"""Точки входа: сначала конфиг из каталога (/etc/cctv), потом импорт модуля.

Порядок важен: модули движка читают CCTV_* при импорте, поэтому
``python -m cctv.engine.cctv_bridge`` в обход этих функций конфиг-каталог не видит.
"""
from __future__ import annotations

import importlib
import sys

from . import settings

COMMANDS = {
    "bridge": ("engine", "cctv.engine.cctv_bridge"),
    "pipeline": ("engine", "cctv.engine.cctv_pipeline"),
    "provision": ("engine", "cctv.engine.cctv_provision"),
    "rtsp-proxy": ("engine", "cctv.engine.rtsp_credential_proxy"),
    "bot": ("bot", "cctv.bot.main"),
    "notify": ("bot", "cctv.notify"),
    "diag-summary": ("engine", "cctv.engine.person_diag"),
}


def run(command: str, argv: list[str] | None = None) -> int:
    component, module_name = COMMANDS[command]
    try:
        settings.apply(component)
    except settings.SettingsError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    if command in ("notify", "diag-summary"):
        return int(module.main(argv) or 0)
    return int(module.main() or 0)


def bridge() -> int:
    return run("bridge")


def pipeline() -> int:
    return run("pipeline")


def provision() -> int:
    return run("provision")


def rtsp_proxy() -> int:
    return run("rtsp-proxy")


def bot() -> int:
    return run("bot")


def notify() -> int:
    return run("notify", sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMMANDS:
        print("usage: python -m cctv {" + "|".join(COMMANDS) + "} [args]", file=sys.stderr)
        return 64
    return run(argv[0], argv[1:])
