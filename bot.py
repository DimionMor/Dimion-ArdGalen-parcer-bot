#!/usr/bin/env python3
"""
Telegram-бот для мониторинга kad.arbitr.ru по ИНН.
Использует Playwright для обхода защиты сайта.
Хостинг: Railway.
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from playwright.async_api import async_playwright

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = int(os.environ["CHAT_ID"])

INN        = "7813322470"
STATE_FILE = Path("state.json")

CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", 168))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Парсинг через Playwright ─────────────────────────────────────────────────

async def fetch_cases(inn: str) -> list[dict]:
    """
    Открывает kad.arbitr.ru в headless-браузере, дожидается cookies,
    затем делает API-запрос уже с правильной сессией.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        log.info("Открываю kad.arbitr.ru для получения сессии...")
        await page.goto("https://kad.arbitr.ru/", wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)  # ждём установки cookies защиты

        log.info("Отправляю поисковый запрос по ИНН %s", inn)
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

        response = await page.evaluate(
            """async (payload) => {
                const resp = await fetch('/Kad/SearchInstances', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify(payload),
                });
                return await resp.json();
            }""",
            payload,
        )

        await browser.close()

    items = response.get("Result", {}).get("Items", []) if response.get("Success") else []
    log.info("Получено дел: %d", len(items))
    return items


# ─── Состояние ────────────────────────────────────────────────────────────────

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

async def check_and_notify(bot, notify: bool = True) -> str:
    log.info("Запускаю проверку для ИНН %s", INN)
    try:
        cases = await fetch_cases(INN)
    except Exception as e:
        log.error("Ошибка: %s", e)
        return f"⚠️ Ошибка при запросе к kad.arbitr.ru:\n`{e}`"

    known   = load_state()
    current = {c.get("CaseId") for c in cases if c.get("CaseId")}
    new_ids = current - known
    total   = len(current)
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not new_ids:
        log.info("Новых дел не обнаружено.")
        return (
            f"📊 *Отчёт по ИНН* `{INN}`\n"
            f"🗓 {timestamp}\n\n"
            f"📁 Всего дел в базе: *{total}*\n"
            f"✅ Новых дел нет"
        )

    new_cases = [c for c in cases if c.get("CaseId") in new_ids]
    log.info("Найдено новых дел: %d", len(new_cases))

    header = (
        f"📊 *Отчёт по ИНН* `{INN}`\n"
        f"🗓 {timestamp}\n\n"
        f"📁 Всего дел в базе: *{total}*\n"
        f"🔔 Обнаружено новых дел: *{len(new_cases)}*\n\n"
    )
    body = "\n\n".join(format_case(c) for c in new_cases)
    text = header + body

    if notify:
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
    result = await check_and_notify(ctx.bot, notify=False)
    await update.message.reply_text(result, parse_mode="Markdown")


# ─── Keep-alive HTTP для Railway ──────────────────────────────────────────────

async def start_http_server() -> None:
    port = int(os.environ.get("PORT", 8080))

    async def healthcheck(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("HTTP keep-alive на порту %d", port)


# ─── Точка входа ──────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
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
