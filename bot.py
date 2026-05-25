#!/usr/bin/env python3
"""
Telegram-бот для мониторинга kad.arbitr.ru по ИНН.
Playwright — регистрирует перехват ДО goto, кликает форму как человек.
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
import pytz
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = int(os.environ["CHAT_ID"])
INN        = "7813322470"
COMPANY    = "АРД-ГАЛЕН"
STATE_FILE = Path("state.json")
CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", 168))

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


async def fetch_cases() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = await context.new_page()

        # Регистрируем перехват ДО открытия страницы
        cases_result = []
        search_done = asyncio.Event()

        async def on_response(response):
            if "SearchInstances" in response.url:
                log.info("SearchInstances перехвачен! Статус: %d", response.status)
                try:
                    data = await response.json()
                    log.info("Success=%s, Items=%s", data.get("Success"),
                             len(data.get("Result", {}).get("Items", [])) if data.get("Result") else 0)
                    if data.get("Success"):
                        items = data.get("Result", {}).get("Items", [])
                        cases_result.extend(items)
                except Exception as e:
                    log.error("Ошибка парсинга: %s", e)
                finally:
                    search_done.set()

        page.on("response", on_response)

        log.info("Открываю kad.arbitr.ru...")
        await page.goto("https://kad.arbitr.ru/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        log.info("Заголовок: %s", await page.title())

        # Вводим название компании
        try:
            inn_field = await page.wait_for_selector(
                "input[placeholder*='название']",
                timeout=15000,
            )
            await inn_field.click()
            await asyncio.sleep(0.5)
            await inn_field.type(COMPANY, delay=150)
            log.info("Название введено: %s", COMPANY)
            await asyncio.sleep(3)

            # Кликаем первый вариант из выпадающего списка
            suggestion = await page.query_selector(".tt-suggestion, li.tt-suggestion, .suggestions-item")
            if suggestion:
                await suggestion.click()
                log.info("Кликнул на подсказку")
            else:
                # Логируем HTML выпадающего списка для диагностики
                dropdown_html = await page.evaluate("""
                    () => {
                        const els = document.querySelectorAll('[class*="suggest"], [class*="dropdown"], [class*="autocomplete"], .tt-menu');
                        return Array.from(els).map(e => e.outerHTML.substring(0, 200)).join('\\n---\\n');
                    }
                """)
                log.info("Dropdown HTML: %s", dropdown_html[:500] if dropdown_html else "пусто")
                await inn_field.press("ArrowDown")
                await asyncio.sleep(0.3)
                await inn_field.press("Enter")
                log.info("Enter — выбрал через клавиатуру")

            await asyncio.sleep(1)

            # Нажимаем Найти
            await page.click("button:has-text('Найти')")
            log.info("Нажал Найти")

        except Exception as e:
            log.error("Ошибка формы: %s", e)

        # Ждём SearchInstances
        try:
            await asyncio.wait_for(search_done.wait(), timeout=30)
            log.info("Итого дел: %d", len(cases_result))
        except asyncio.TimeoutError:
            log.warning("Таймаут — SearchInstances не перехвачен")

        await browser.close()

    return cases_result


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
        cases = await fetch_cases()
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
    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Europe/Moscow"))
    scheduler.add_job(
        lambda: asyncio.ensure_future(check_and_notify(application.bot)),
        trigger="cron",
        day_of_week="wed",
        hour=12,
        minute=0,
        id="weekly_check",
        max_instances=1,
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
