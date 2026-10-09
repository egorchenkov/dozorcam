"""Мок Telegram Bot API и GitHub Releases для install-smoke (без сети и без настоящего бота).

Telegram: ``/bot<TOKEN>/<method>`` — getMe отдаёт бота ``--bot``, getUpdates держит
long poll до 2 с и отдаёт пусто, остальные методы — ``true``; чужой токен — 401.
GitHub: ``/github/repos/<owner>/<repo>/releases/latest`` — ``tag_name`` из ``--latest``.
Каждый запрос — строка ``METHOD путь`` (без токена) в ``--log``.
"""
from __future__ import annotations

import argparse
import http.server
import json
import threading
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--token", required=True)
    parser.add_argument("--bot", default="dozorcam_smoke_bot")
    parser.add_argument("--latest", default="")
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # бот остановили посреди long poll

        def handle_any(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            path = self.path.split("?")[0]
            with lock, open(args.log, "a", encoding="utf-8") as handle:
                handle.write(f"{self.command} {path.replace(args.token, '<token>')}\n")
            if path.startswith("/github/") and path.endswith("/releases/latest"):
                if not args.latest:
                    return self.reply(404, {"message": "Not Found"})
                return self.reply(200, {"tag_name": f"v{args.latest}", "name": args.latest})
            prefix = f"/bot{args.token}/"
            if not path.startswith(prefix):
                return self.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            method = path[len(prefix):].lower()
            if method == "getme":
                return self.reply(200, {"ok": True, "result": {
                    "id": int(args.token.split(":")[0]), "is_bot": True, "first_name": "Smoke",
                    "username": args.bot, "can_join_groups": True,
                    "can_read_all_group_messages": False, "supports_inline_queries": False}})
            if method == "getupdates":
                time.sleep(2)
                return self.reply(200, {"ok": True, "result": []})
            return self.reply(200, {"ok": True, "result": True})

        do_GET = do_POST = handle_any

        def log_message(self, *_args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
