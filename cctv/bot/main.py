#!/usr/bin/env python3
"""Точка входа бота (cctv-bot; конфиг из каталога — см. cctv.cli и cctv.settings).

Отдельный процесс, отдельное состояние. Ни LLM, ни чужих рантаймов: бот
детерминированный и говорит только с Telegram и мостом.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import config
from .bot import CctvBot
from .bridge import Bridge
from .events import EventServer
from .state import State

HOUSEKEEPING_INTERVAL_SEC = 3600
# Панель — «онлайн»-индикатор: без периодической перерисовки остановившийся
# детектор так и остался бы в теме зелёным до следующего нажатия.
PANEL_REFRESH_INTERVAL_SEC = 60
# Сторож смотрит реестр реже панелей: минута тишины ещё не поломка, а вот
# четверть часа без кадров — уже новость, которую нужно сказать вслух.
WATCHDOG_INTERVAL_SEC = 120
TG_TIMEOUT_SEC = 20

log = logging.getLogger("cctv-tg-bot")


def _forbid_foreign_secrets(cfg) -> None:
    """Страховка границы: процессу не полагаются ни proxy, ни ключи чужих ботов."""
    import os

    prefixes = ("ANTHROPIC_", "OPENAI_", "CLAUDE_BOT_", "LLM_PROXY")
    exact = ("TELEGRAM_BOT_TOKEN", "HTTP_PROXY", "HTTPS_PROXY")
    leaked = sorted(
        name for name in os.environ if name.startswith(prefixes) or name in exact
    )
    if leaked:
        raise config.ConfigError("foreign_secrets", names=", ".join(leaked))


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx на INFO печатает URL каждого запроса целиком, а у Bot API токен —
    # часть пути: журнал сервиса становился постоянным хранилищем секрета
    # (journald здесь персистентный) и заодно выдавал внутренний адрес Bridge.
    # Сетевой слой говорит только о настоящих сбоях.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = config.load()
    _forbid_foreign_secrets(cfg)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)

    from telegram.ext import ApplicationBuilder

    state = State(str(cfg.db_path))
    bridge = Bridge(cfg)
    application = (ApplicationBuilder().token(cfg.bot_token)
                   # Ответ Telegram на sendMessage со стенда — до 5–6 с: при дефолтных
                   # 5 с PTB мастер ловил TimedOut на уже отправленных сообщениях.
                   .read_timeout(TG_TIMEOUT_SEC).write_timeout(TG_TIMEOUT_SEC).build())
    core = CctvBot(cfg, state, bridge, application.bot, log=log.info)

    register_handlers(application, core, state)

    loop = asyncio.get_running_loop()
    server = None
    if cfg.events_enabled:
        def submit(event):
            future = asyncio.run_coroutine_threadsafe(core.on_event(event), loop)

            def report(done):
                # Без этого исключение внутри обработки события оседало в
                # невостребованном future: событие тихо терялось, а тема
                # выглядела спокойной. Архив живёт в теме — молчать нельзя.
                exc = done.exception()
                if exc is not None:
                    log.error("событие не обработано: %s: %s", type(exc).__name__, exc)

            future.add_done_callback(report)

        server = EventServer(cfg, submit, log_line=log.info)
        server.serve_in_thread()
        log.info("приёмник событий слушает %s:%s (%s)", cfg.events_host, cfg.events_port,
                 "mTLS" if cfg.internal_tls else "loopback без TLS")

    # Уборка своим таском, а не job_queue: тот существует только с APScheduler
    # (падало на старте 24.08.2026, когда его не было в окружении). Тащить
    # зависимость ради часового таймера дороже, чем цикл на семь строк.
    async def housekeeping():
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_SEC)
            state.purge_expired_callbacks()
            state.purge_expired_frames()
            state.purge_seen_events(cfg.event_dedup_ttl_sec)
            # Сутки — с запасом дольше любого честного пути media.ready/failed.
            state.purge_stale_requests(86400)

    async def panel_refresh():
        while True:
            await asyncio.sleep(PANEL_REFRESH_INTERVAL_SEC)
            try:
                # Реестр может пополниться уже после старта (камера подключилась
                # к Bridge позже бота). Одних панелей мало: они не создают тему.
                await core.sync_registry()
                await core.refresh_all_panels()
            except Exception as exc:  # индикатор не должен ронять сервис
                log.info("обновление панелей отложено: %s", type(exc).__name__)

    async def watchdog():
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
            try:
                await core.watch_health()
            except Exception as exc:  # сторож не должен ронять сервис
                log.info("сторож отложен: %s", type(exc).__name__)

    # Набор кнопок панели меняется вместе с версией бота, а её текст — нет:
    # без принудительной перерисовки в темах остались бы кнопки прошлой версии.
    await core.sync_registry()
    await core.refresh_all_panels(force=True)
    await core.ensure_topic_icons()

    chores = asyncio.create_task(housekeeping())
    panels = asyncio.create_task(panel_refresh())
    health = asyncio.create_task(watchdog())

    try:
        async with application:
            await announce_setup(core, application.bot)
            await application.start()
            # my_chat_member нужен мастеру: «бота добавили в группу / сделали админом».
            await application.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query", "my_chat_member"])
            await asyncio.Event().wait()
    finally:
        chores.cancel()
        panels.cancel()
        health.cancel()
        if server is not None:
            server.shutdown()
        bridge.close()
        state.close()
    return 0


def register_handlers(application, core, state) -> None:
    """Обработчики Telegram. Отдельно от _amain: сквозной прогон мастера
    на стенде кормит те же обработчики синтетическими апдейтами."""
    from telegram import Update
    from telegram.ext import (CallbackQueryHandler, ChatMemberHandler, CommandHandler,
                              MessageHandler, TypeHandler, filters)

    async def reply(message, text):
        if text:
            await message.reply_text(text)

    async def on_start(update, ctx):
        """/start <код> в личке — мастер; в группе — привязка либо меню, как раньше."""
        user, chat, message = update.effective_user, update.effective_chat, update.effective_message
        if chat is not None and chat.type != "private" and core.chat_id == chat.id:
            return await on_menu(update, ctx)
        await reply(message, await core.on_start(
            user.id if user else None, chat.id if chat else None,
            chat.type if chat else "private", list(ctx.args or []),
            getattr(user, "language_code", None)))

    async def on_setup(update, _ctx):
        user, chat, message = update.effective_user, update.effective_chat, update.effective_message
        if chat is None or chat.type == "private" or not core.allowed(user.id if user else None):
            return
        await reply(message, await core.bind_group(chat.id, user.id))

    async def on_add(update, ctx):
        user, message = update.effective_user, update.effective_message
        if not core.allowed(user.id if user else None):
            return
        await reply(message, await core.on_add(user.id, list(ctx.args or [])))

    async def on_lang(update, ctx):
        user, message = update.effective_user, update.effective_message
        if not core.allowed(user.id if user else None):
            return
        await reply(message, await core.set_language(user.id, list(ctx.args or [])))

    async def on_model(update, _ctx):
        user, message = update.effective_user, update.effective_message
        if not core.allowed(user.id if user else None):
            return
        await reply(message, await core.on_model(user.id))

    async def on_membership(update, _ctx):
        change = update.my_chat_member
        if change is None:
            return
        await core.on_bot_membership(change.chat.id, change.chat.type,
                                     change.from_user.id if change.from_user else None,
                                     change.new_chat_member.status)

    async def on_migrate(update, _ctx):
        message = update.effective_message
        if message is not None and message.migrate_to_chat_id:
            await core.on_chat_migrated(message.chat_id, message.migrate_to_chat_id)

    async def on_menu(update, _ctx):
        user = update.effective_user
        if not core.allowed(user.id if user else None):
            return
        message = update.effective_message
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None and state.camera_for_thread(thread_id):
            await message.reply_text(await core.show_keyboard(thread_id),
                                     reply_markup=core.reply_keyboard())
            return
        await message.reply_text(await core.menu_text(), reply_markup=core.reply_keyboard())

    async def on_button(update, _ctx):
        query = update.callback_query
        message = query.message
        answer = await core.on_callback(
            query.from_user.id if query.from_user else None,
            getattr(message, "message_thread_id", None),
            query.data,
        )
        # Тост Telegram — одна строка и максимум 200 символов: многострочный
        # статус показываем всплывающим окном и режем по лимиту API.
        multiline = "\n" in answer
        await query.answer(answer[:200], show_alert=multiline)

    async def on_text(update, ctx):
        user = update.effective_user
        if not core.allowed(user.id if user else None):
            return
        message = update.effective_message
        thread_id = getattr(message, "message_thread_id", None)
        # В теме форума каждое сообщение несёт служебный reply на корень темы —
        # настоящим ответом считается только reply на другое сообщение.
        reply_id = getattr(getattr(message, "reply_to_message", None), "message_id", None)
        if reply_id is not None and reply_id == thread_id:
            reply_id = None
        answer = await core.on_text(
            user.id if user else None,
            thread_id,
            message.text or "",
            reply_id,
            message.message_id,
        )
        try:
            await message.reply_text(answer)
        except Exception:
            # Сообщение с паролем удаляется до ответа: reply на него уже
            # невозможен, и ответ уходит в ту же тему обычным сообщением.
            await ctx.bot.send_message(chat_id=message.chat_id,
                                       message_thread_id=thread_id, text=answer)

    async def on_any(update, _ctx):
        """Язык Telegram владельца — подсказка языка бота, пока не выбран явно."""
        user = update.effective_user
        if user is not None:
            core.note_language(user.id, getattr(user, "language_code", None))

    # Группа −1: смотрит каждое обновление раньше остальных и никого не блокирует.
    application.add_handler(TypeHandler(Update, on_any), group=-1)
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("menu", on_menu))
    application.add_handler(CommandHandler("setup", on_setup))
    application.add_handler(CommandHandler("add", on_add))
    application.add_handler(CommandHandler("lang", on_lang))
    application.add_handler(CommandHandler("model", on_model))
    application.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    application.add_handler(CallbackQueryHandler(on_button, pattern=r"^cv:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


async def announce_setup(core, bot) -> None:
    """Код владельца — в журнал (docker logs): единственный канал до /start.

    Меню команд ставим на каждом старте: оно зависит от языка Telegram у
    пользователя, а не от языка бота.
    """
    from telegram import BotCommand

    from .. import i18n

    for lang in ("en", "ru"):
        commands = [BotCommand(name, i18n.t(f"command.{name}", lang))
                    for name in ("menu", "add", "model", "lang", "setup")]
        try:
            await bot.set_my_commands(commands, language_code=None if lang == "en" else lang)
        except Exception as exc:  # меню — удобство, а не условие запуска
            log.info("меню команд (%s) не установлено: %s", lang, type(exc).__name__)
    code = core.setup_code()
    if code is None:
        return
    username = getattr(bot, "username", "") or ""
    link = f"https://t.me/{username}?start={code}" if username else ""
    log.info("=" * 60)
    log.info("SETUP: owner code %s — send /start %s to the bot in a private chat%s",
             code, code, f" or open {link}" if link else "")
    log.info("МАСТЕР: код владельца %s — /start %s боту в личку%s",
             code, code, f" или ссылка {link}" if link else "")
    log.info("=" * 60)


def main() -> int:
    try:
        return asyncio.run(_amain())
    except config.ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
