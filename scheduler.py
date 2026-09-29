"""Scheduled, idempotent report delivery."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import avito_api
import config
import database
from reports import (
    daily_report_periods,
    format_low_balance_client_alert,
    format_low_balance_team_alert,
    format_daily_report,
    format_period_report,
    report_period,
)

logger = logging.getLogger(__name__)


async def _notify_admins(bot: Bot, text: str) -> None:
    for chat_id in config.ADMIN_ALERT_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            logger.exception("Не удалось уведомить администратора %s", chat_id)


async def build_report(store, report_type: str, as_of: date) -> str:
    client_id, client_secret = await database.get_store_credentials(store)
    token = await avito_api.get_avito_token(client_id, client_secret)
    user_id = store["user_id"] or await avito_api.get_avito_user_id(token)
    if report_type == "daily":
        yesterday, week_start, week_end, previous_week_start, previous_week_end = daily_report_periods(as_of)
        date_from, date_to = previous_week_start, week_end
    else:
        current_from, current_to, previous_from, previous_to = report_period(report_type, as_of)
        date_from, date_to = previous_from, current_to

    # Promo v2 returns views, total contacts and contactsMessenger together.
    # One request avoids rate-limit collisions between separate stats calls.
    daily = await avito_api.get_daily_promo_stats(token, int(user_id), date_from, date_to)
    item_ids = await avito_api.get_all_item_ids(token)
    calls = await avito_api.get_daily_calls(token, int(user_id), item_ids, date_from, date_to)
    spendings = await avito_api.get_daily_spendings(token, int(user_id), date_from, date_to)
    for day, value in calls.items():
        daily.setdefault(day, {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0})["calls"] = value
    for day, value in spendings.items():
        daily.setdefault(day, {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0})["spend"] = value
    balance = await avito_api.get_balance(token, int(user_id))
    if report_type == "daily":
        yesterday_stats = avito_api.sum_period(daily, yesterday, yesterday)
        week_stats = avito_api.sum_period(daily, week_start, week_end)
        previous_week_stats = avito_api.sum_period(daily, previous_week_start, previous_week_end)
        return format_daily_report(
            store["store_name"], yesterday, yesterday_stats, week_start, week_end,
            week_stats, previous_week_stats, balance,
        )
    current = avito_api.sum_period(daily, current_from, current_to)
    previous = avito_api.sum_period(daily, previous_from, previous_to)
    return format_period_report(store["store_name"], report_type, current_from, current_to, current, previous, balance)


async def _telegram_send_with_retry(bot: Bot, chat_id: int, text: str):
    last_error = None
    for attempt in range(3):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except TelegramAPIError as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2**attempt)
    raise last_error  # type: ignore[misc]


async def send_project_report(
    bot: Bot,
    store,
    report_type: str,
    as_of: date,
    *,
    test: bool = False,
) -> bool:
    current_from, current_to, _, _ = report_period(report_type, as_of)
    delivery_id = None
    if not test:
        delivery_id = await database.claim_delivery(
            store["id"], report_type, current_from.isoformat(), current_to.isoformat()
        )
        if delivery_id is None:
            logger.info("Отчёт уже отправлен: %s, %s", store["store_name"], report_type)
            return True

    try:
        text = await build_report(store, report_type, as_of)
        if test:
            text = "🧪 <b>Тестовая отправка</b>\n\n" + text
        message = await _telegram_send_with_retry(bot, store["chat_id"], text)
        if delivery_id is not None:
            await database.mark_delivery_sent(delivery_id, message.message_id)
        logger.info("Отчёт %s для %s отправлен", report_type, store["store_name"])
        return True
    except Exception as exc:
        logger.exception("Ошибка отчёта %s для %s", report_type, store["store_name"])
        if delivery_id is not None:
            await database.mark_delivery_failed(delivery_id, str(exc))
        # A manually launched test is expected to be retried by the operator.
        # Do not pollute the avitologists' operational chat with transient
        # Avito rate-limit errors from such a test. Scheduled deliveries still
        # notify the team about every real failure.
        if not test:
            await _notify_admins(
                bot,
                f"⚠️ Не отправлен {report_type}-отчёт для «{store['store_name']}»: {str(exc)[:800]}",
            )
        return False


async def scheduled_report_job(bot: Bot, report_type: str) -> None:
    as_of = local_today()
    stores = await database.get_enabled_stores(report_type)
    logger.info("Запуск %s рассылки: %d проектов", report_type, len(stores))
    for store in stores:
        await send_project_report(bot, store, report_type, as_of)


def _is_reminder_due(last_alert_at: str | None) -> bool:
    if not last_alert_at:
        return True
    try:
        alert_at = datetime.fromisoformat(last_alert_at)
        if alert_at.tzinfo is None:
            alert_at = alert_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= alert_at + timedelta(hours=config.LOW_BALANCE_REMINDER_HOURS)


async def check_low_balances_job(bot: Bot) -> None:
    """Notify clients on threshold crossing and send one aggregate team alert."""
    stores = await database.get_balance_monitored_stores()
    team_items: list[tuple[str, str, dict[str, float]]] = []
    for store in stores:
        try:
            client_id, client_secret = await database.get_store_credentials(store)
            token = await avito_api.get_avito_token(client_id, client_secret)
            user_id = store["user_id"] or await avito_api.get_avito_user_id(token)
            balance = await avito_api.get_balance(token, int(user_id))
            is_low = balance["total"] < config.LOW_BALANCE_THRESHOLD
            if not is_low:
                if store["low_balance_is_low"]:
                    logger.info("Баланс проекта %s восстановился", store["store_name"])
                await database.update_low_balance_state(store["id"], is_low=False)
                continue

            needs_alert = not bool(store["low_balance_is_low"]) or _is_reminder_due(
                store["low_balance_last_alert_at"]
            )
            if not needs_alert:
                continue
            text = format_low_balance_client_alert(
                store["store_name"], store["client_mention"], balance
            )
            await _telegram_send_with_retry(bot, store["chat_id"], text)
            await database.update_low_balance_state(store["id"], is_low=True, alert_sent=True)
            team_items.append((store["store_name"], store["client_mention"], balance))
            logger.warning("Отправлено предупреждение о низком балансе: %s", store["store_name"])
        except Exception as exc:
            logger.exception("Не удалось проверить баланс проекта %s", store["store_name"])
            await _notify_admins(
                bot, f"⚠️ Не проверен баланс «{store['store_name']}»: {str(exc)[:800]}"
            )

    if team_items and config.LOW_BALANCE_ALERT_CHAT_ID:
        try:
            await _telegram_send_with_retry(
                bot,
                config.LOW_BALANCE_ALERT_CHAT_ID,
                format_low_balance_team_alert(team_items),
            )
        except Exception:
            logger.exception("Не удалось отправить сводку низких балансов авитологам")
            await _notify_admins(bot, "⚠️ Не отправлена сводка низких балансов в чат авитологов")


def local_today() -> date:
    from datetime import datetime

    return datetime.now(ZoneInfo(config.TIMEZONE)).date()
