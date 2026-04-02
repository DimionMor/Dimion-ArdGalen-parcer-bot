#!/usr/bin/env python3
"""
Telegram-бот для мониторинга kad.arbitr.ru по ИНН.
Хостинг: Railway.
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = int(os.environ["CHAT_ID"])
INN        = "7813322470"
STATE_FILE = Path("state.json")
CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", 168))

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Точно такие же заголовки как в рабочем botsudparser
HEADERS_GET = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://kad.arbitr.ru/",
}

HEADERS_POST = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://kad.arbitr.ru",
    "Referer": "https://kad.arbitr.ru/",
    "Connection": "keep-alive",
}


async def fetch_cases(inn: str) -> list[dict]:
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        cookie_jar=aiohttp.CookieJar(),
    ) as session:

        # Шаг 1: GET главной страницы — получаем куки
        log.info("Открываю главную kad.arbitr.ru...")
        async with session.get(
            "https://kad.arbitr.ru/", headers=HEADERS_GET
        ) as r:
            await r.read()
            log.info("GET главной: %d", r.status)

        await asyncio.sleep(3)

        # Шаг 2: POST запрос с куками сессии
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
            "WithVKSInstances": False,
            "CaseType": "",
        }

        log.info("POST SearchInstances для ИНН %s...", inn)
        async with session.post(
            "https://kad.arbitr.ru/Kad/SearchInstances",
            json=payload,
            headers=HEADERS_POST,
        ) as r:
            log.info("POST ответ: %d", r.status)
            text = await r.text()

        if r.status != 200:
            raise Exception(f"HTTP {r.status}: {text[:400]}")

        data = json.loads(text)

    if not data.get("Success"):
        log.warning("Success=false: %s", data.get("Message"))
        return []

    return data.get("Result", {}).get("Items", [])


def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_state(ids: set):
    STATE_FILE.write_text(json.dumps(list(ids)))


def format_case(case: dict) -> str:
    number   = case.get("CaseId", "—")
    date     = (case.get("Date") or "")[:10] or "—"
    court    = case.get("Court", {}).get("Name", "—")
    claimant = ", ".join(
        s.get("Name", "") for s in case.get("Sides", [])
        if s.get("SideType") == "Заявитель"
    ) or "—"
    url = f"https://kad.arbitr.ru/Card/{case.get('CaseId','')}"
    return (
        f"📋 *Дело {number}*\n"
        f"📅 Дата: {date}\n"
        f"🏛 Суд: {court}\n"
        f"👤 Заявитель: {claimant}\n"
        f"🔗 [Открыть]({url})"
    )


async def check_and_notify(bot, notify: bool = True) -> str:
    log.info("Проверка ИНН %s", INN)
    try:
        cases = await fetch_cases(INN)
    except Exception as e:
        log.error("Ошибка: %s", e)
        return f"⚠️ Ошибка:\n`{e}`"

    known   = load_state()
    current = {c.get("CaseId") for c in cases if c.get("CaseId")}
    new_ids = current - known
    total   = len(current)
    ts      = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not new_ids:
        return (
            f"📊 *Отчёт по ИНН* `{INN}`\n"
            f"🗓 {ts}\n\n"
            f"📁 Всего дел: *{total}*\n"
            f"✅ Новых дел нет"
        )

    new_cases = [c for c in cases if c.get("CaseId") in new_ids]
    text = (
        f"📊 *Отчёт по ИНН* `{INN}`\n"
        f"🗓 {ts}\n\n"
        f"📁 Всего дел: *{total}*\n"
        f"🔔 Новых: *{len(new_cases)}*\n\n"
        + "\n\n".join(format_case(c) for c in new_cases)
    )
    if notify:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
    save_state(known | new_ids)
    return text


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Мониторинг kad.arbitr.ru\nИНН: `{INN}`\n/report — проверить сейчас",
        parse_mode="Markdown",
    )


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Запрашиваю данные...")
    result = await check_and_notify(ctx.bot, notify=False)
    await update.message.reply_text(result, parse_mode="Markdown")


async def start_http_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP на порту %d", port)


async def post_init(application: Application):
    await start_http_server()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.ensure_future(check_and_notify(application.bot)),
        trigger="interval", hours=CHECK_INTERVAL_HOURS,
        id="weekly_check", max_instances=1,
    )
    scheduler.start()
    log.info("Бот запущен. ИНН: %s", INN)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
