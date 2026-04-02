#!/usr/bin/env python3
"""
Telegram-бот для мониторинга kad.arbitr.ru по ИНН.
Проверяет новые арбитражные дела раз в неделю.
Хостинг: Railway (aiohttp keep-alive на $PORT).
"""

import os
import json
import logging
import asyncio
import httpx
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN  = os.environ["BOT_TOKEN"]    # токен от @BotFather
CHAT_ID    = int(os.environ["CHAT_ID"]) # ваш Telegram chat_id

INN        = "7813322470"               # ИНН отслеживаемой организации
STATE_FILE = Path("state.json")         # хранит ID уже известных дел

# Интервал проверки в часах (168 = 1 раз в неделю)
CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", 168))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Работа с kad.arbitr.ru ───────────────────────────────────────────────────

KAD_URL = "https://kad.arbitr.ru/Kad/SearchInstances"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; KadMonitorBot/1.0)",
    "Referer": "https://kad.arbitr.ru/",
    "X-Requested-With": "XMLHttpRequest",
}


async def fetch_cases(inn: str) -> list[dict]:
    """Запрашивает список дел по ИНН через API kad.arbitr.ru."""
    payload = {
        "Page": 1,
        "Count": 50,
        "Courts": [],
        "DateFrom": None,
        "DateTo": None,
        "Sides": [{"Name": "", "Inn": inn, "Type": "participant"}],
        "Judges": [],
        "CaseNumbers": [],
        "Keywords": "",
        "CaseType": "",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(KAD_URL, headers=HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data.get("Result", {}).get("Items", [])


def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_state(case_ids: set) -> None:
    STATE_FILE.write_text(json.dumps(list(case_ids)))


def format_case(case: dict) -> str:
    number   = case.get("CaseId", "—")
    date     = case.get("Date", "")[:10] if case.get("Date") else "—"
    court    = case.get("Court", {}).get("Name", "—")
    claimant = ", ".join(
        s.get("Name", "") for s in case.get("Sides", [])
        if s.get("SideType") == "Заявитель"
    ) or "—"
    url = f"https://kad.arbitr.ru/Card/{case.get('CaseId', '')}"
    return (
        f"📋 *Дело {number}*\n"
        f"📅 Дата: {date}\n"
        f"🏛 Суд: {court}\n"
        f"👤 Заявитель: {claimant}\n"
        f"🔗 [Открыть на kad.arbitr.ru]({url})"
    )


# ─── Проверка ─────────────────────────────────────────────────────────────────

async def check_and_notify(bot) -> str:
    log.info("Запускаю проверку для ИНН %s", INN)
    try:
        cases = await fetch_cases(INN)
    except Exception as e:
        log.error("Ошибка запроса к kad.arbitr.ru: %s", e)
        return f"⚠️ Ошибка при запросе к kad.arbitr.ru:\n`{e}`"

    known   = load_state()
    current = {c.get("CaseId") for c in cases if c.get("CaseId")}
    new_ids = current - known

    if not new_ids:
        log.info("Новых дел не обнаружено.")
        return (
            f"✅ Новых дел для ИНН `{INN}` не найдено.\n"
            f"_Проверено: {datetime.now().strftime('%d.%m.%Y %H:%M')}_"
        )

    new_cases = [c for c in cases if c.get("CaseId") in new_ids]
    log.info("Найдено новых дел: %d", len(new_cases))

    header = (
        f"🔔 *Новые дела на kad.arbitr.ru*\n"
        f"ИНН: `{INN}` | {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        f"Найдено новых: *{len(new_cases)}*\n\n"
    )
    body = "\n\n".join(format_case(c) for c in new_cases)
    text = header + body

    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
    save_state(known | new_ids)
    return text


# ─── Команды Telegram ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"👋 Бот мониторинга kad.arbitr.ru запущен.\n"
        f"ИНН: `{INN}`\n"
        f"Проверка каждые {CHECK_INTERVAL_HOURS} ч.\n\n"
        f"Команды:\n/report — проверить прямо сейчас",
        parse_mode="Markdown",
    )


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Запрашиваю данные, подождите...")
    result = await check_and_notify(ctx.bot)
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text(result, parse_mode="Markdown")
    else:
        await update.message.reply_text("✔️ Проверка завершена.", parse_mode="Markdown")


# ─── Keep-alive HTTP-сервер для Railway ───────────────────────────────────────

async def start_http_server() -> None:
    """
    Railway требует открытый HTTP-порт — иначе считает деплой упавшим.
    Поднимаем минимальный healthcheck-эндпоинт на $PORT.
    """
    port = int(os.environ.get("PORT", 8080))

    async def healthcheck(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("HTTP keep-alive запущен на порту %d", port)


# ─── Точка входа ──────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Запускается внутри event loop после инициализации бота."""
    await start_http_server()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.ensure_future(check_and_notify(application.bot)),
        trigger="interval",
        hours=CHECK_INTERVAL_HOURS,
        id="weekly_check",
        max_instances=1,
    )
    scheduler.start()
    log.info("Планировщик запущен. Интервал: %d ч.", CHECK_INTERVAL_HOURS)
    log.info("Бот запущен. ИНН: %s", INN)


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
