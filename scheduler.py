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
    format_low_balance_client_alert,
    format_low_balance_team_alert,
    format_report,
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
    current_from, current_to, previous_from, previous_to = report_period(report_type, as_of)
    client_id, client_secret = await database.get_store_credentials(store)
    token = await avito_api.get_avito_token(client_id, client_secret)
    user_id = store["user_id"] or await avito_api.get_avito_user_id(token)
    daily = await avito_api.get_daily_stats(token, int(user_id), previous_from, current_to)
    current = avito_api.sum_period(daily, current_from, current_to)
    previous = avito_api.sum_period(daily, previous_from, previous_to)
    balance = await avito_api.get_balance(token, int(user_id))
    return format_report(
        store["store_name"], report_type, current_from, current_to, current, previous, balance
    )


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
