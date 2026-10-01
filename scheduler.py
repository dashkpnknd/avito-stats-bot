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
    format_daily_team_summary,
    format_low_balance_client_alert,
    format_low_balance_team_alert,
    format_daily_report,
    format_period_report,
    format_weekly_team_summary,
    report_period,
)

logger = logging.getLogger(__name__)

# APScheduler starts the daily and monthly jobs independently. Serialising them
# prevents the two report types from exhausting Avito's shared API quota at
# 10:00 on the first day of a month.
_REPORT_JOB_LOCK = asyncio.Lock()


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


async def build_daily_report_with_summary(store, as_of: date) -> tuple[str, dict[str, int | float]]:
    """Build the client report and preserve yesterday's figures for the team.

    Keeping these figures from the same Avito requests avoids a second API pass
    for the operational summary, which is important for rate-limit safety.
    """
    client_id, client_secret = await database.get_store_credentials(store)
    token = await avito_api.get_avito_token(client_id, client_secret)
    user_id = store["user_id"] or await avito_api.get_avito_user_id(token)
    yesterday, week_start, week_end, previous_week_start, previous_week_end = daily_report_periods(as_of)
    daily = await avito_api.get_daily_promo_stats(token, int(user_id), previous_week_start, week_end)
    item_ids = await avito_api.get_all_item_ids(token)
    calls = await avito_api.get_daily_calls(token, int(user_id), item_ids, previous_week_start, week_end)
    spendings = await avito_api.get_daily_spendings(token, int(user_id), previous_week_start, week_end)
    for day, value in calls.items():
        daily.setdefault(day, {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0})["calls"] = value
    for day, value in spendings.items():
        daily.setdefault(day, {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0})["spend"] = value
    balance = await avito_api.get_balance(token, int(user_id))
    yesterday_stats = avito_api.sum_period(daily, yesterday, yesterday)
    week_stats = avito_api.sum_period(daily, week_start, week_end)
    previous_week_stats = avito_api.sum_period(daily, previous_week_start, previous_week_end)
    return (
        format_daily_report(
            store["store_name"], yesterday, yesterday_stats, week_start, week_end,
            week_stats, previous_week_stats, balance,
        ),
        yesterday_stats,
    )


async def build_weekly_report_with_summary(store, as_of: date) -> tuple[str, dict[str, int | float]]:
    """Build the client weekly report and retain its current-week totals."""
    client_id, client_secret = await database.get_store_credentials(store)
    token = await avito_api.get_avito_token(client_id, client_secret)
    user_id = store["user_id"] or await avito_api.get_avito_user_id(token)
    current_from, current_to, previous_from, previous_to = report_period("weekly", as_of)
    daily = await avito_api.get_daily_promo_stats(token, int(user_id), previous_from, current_to)
    item_ids = await avito_api.get_all_item_ids(token)
    calls = await avito_api.get_daily_calls(token, int(user_id), item_ids, previous_from, current_to)
    spendings = await avito_api.get_daily_spendings(token, int(user_id), previous_from, current_to)
    for day, value in calls.items():
        daily.setdefault(day, {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0})["calls"] = value
    for day, value in spendings.items():
        daily.setdefault(day, {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0})["spend"] = value
    balance = await avito_api.get_balance(token, int(user_id))
    current = avito_api.sum_period(daily, current_from, current_to)
    previous = avito_api.sum_period(daily, previous_from, previous_to)
    return (
        format_period_report(store["store_name"], "weekly", current_from, current_to, current, previous, balance),
        current,
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
    summary_items: list[tuple[str, dict[str, int | float]]] | None = None,
    team_details: list[str] | None = None,
    notify_on_failure: bool = True,
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
        yesterday_stats = None
        if report_type == "daily" and summary_items is not None and not test:
            text, yesterday_stats = await build_daily_report_with_summary(store, as_of)
        elif report_type == "weekly" and summary_items is not None and not test:
            text, yesterday_stats = await build_weekly_report_with_summary(store, as_of)
        else:
            text = await build_report(store, report_type, as_of)
        if test:
            text = "🧪 <b>Тестовая отправка</b>\n\n" + text
        message = await _telegram_send_with_retry(bot, store["chat_id"], text)
        if delivery_id is not None:
            await database.mark_delivery_sent(delivery_id, message.message_id)
        if yesterday_stats is not None:
            summary_items.append((store["store_name"], yesterday_stats))
        if team_details is not None and not test:
            team_details.append(text)
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
        if not test and notify_on_failure:
            await _notify_admins(
                bot,
                f"⚠️ Не отправлен {report_type}-отчёт для «{store['store_name']}»: {str(exc)[:800]}",
            )
        return False


async def scheduled_report_job(bot: Bot, report_type: str) -> None:
    if report_type == "monthly" and not is_last_day_of_month(local_today()):
        logger.info("Месячная рассылка пропущена: сегодня не последний день месяца")
        return
    async with _REPORT_JOB_LOCK:
        as_of = local_today()
        stores = await database.get_enabled_stores(report_type)
        logger.info("Запуск %s рассылки: %d проектов", report_type, len(stores))
        if report_type == "weekly":
            await send_weekly_reports_to_team(bot, stores, as_of)
            return
        summary_items: list[tuple[str, dict[str, int | float]]] | None = (
            [] if report_type == "daily" else None
        )
        team_details: list[str] = []
        pending = list(stores)
        # Requests themselves have exponential 429 backoff. This second pass
        # covers a quota outage that outlives a single project attempt, without
        # leaving that project's report until tomorrow/next month.
        for batch_attempt in range(3):
            failed = []
            for store in pending:
                sent = await send_project_report(
                    bot,
                    store,
                    report_type,
                    as_of,
                    summary_items=summary_items,
                    team_details=team_details,
                    notify_on_failure=False,
                )
                if not sent:
                    failed.append(store)
            if not failed:
                pending = []
                break
            pending = failed
            if batch_attempt < 2:
                delay = 60 * (batch_attempt + 1)
                logger.warning(
                    "%s: повтор %d отчётов через %d секунд",
                    report_type, len(pending), delay,
                )
                await asyncio.sleep(delay)

        if pending:
            for store in pending:
                await _notify_admins(
                    bot,
                    f"⚠️ Не отправлен {report_type}-отчёт для «{store['store_name']}» после повторных попыток.",
                )
        if summary_items:
            await send_daily_team_summary(bot, summary_items)
        if team_details:
            await send_team_detailed_reports(bot, team_details)


async def send_daily_team_summary(
    bot: Bot, items: list[tuple[str, dict[str, int | float]]]
) -> None:
    """Publish one compact daily result list after the client reports."""
    if not config.DAILY_SUMMARY_CHAT_ID:
        logger.warning("Сводка по магазинам не отправлена: не настроен чат авитологов")
        return
    try:
        await _telegram_send_with_retry(
            bot, config.DAILY_SUMMARY_CHAT_ID, format_daily_team_summary(items)
        )
        logger.info("Сводка по магазинам отправлена: %d проектов", len(items))
    except Exception:
        logger.exception("Не удалось отправить ежедневную сводку по магазинам")
        await _notify_admins(bot, "⚠️ Не отправлена ежедневная сводка Avito по магазинам")


async def send_weekly_team_summary(
    bot: Bot, items: list[tuple[str, dict[str, int | float]]], as_of: date
) -> None:
    """Publish the completed-week store list after client weekly reports."""
    if not config.DAILY_SUMMARY_CHAT_ID:
        logger.warning("Недельная сводка не отправлена: не настроен чат авитологов")
        return
    date_from, date_to, _, _ = report_period("weekly", as_of)
    try:
        await _telegram_send_with_retry(
            bot,
            config.DAILY_SUMMARY_CHAT_ID,
            format_weekly_team_summary(items, date_from, date_to),
        )
        logger.info("Недельная сводка по магазинам отправлена: %d проектов", len(items))
    except Exception:
        logger.exception("Не удалось отправить недельную сводку по магазинам")
        await _notify_admins(bot, "⚠️ Не отправлена недельная сводка Avito по магазинам")


async def send_team_detailed_reports(bot: Bot, reports: list[str]) -> None:
    """Send already built, client-format reports only to the team chat."""
    if not config.DAILY_SUMMARY_CHAT_ID:
        logger.warning("Подробные отчёты не отправлены: не настроен чат авитологов")
        return
    for report in reports:
        await _telegram_send_with_retry(bot, config.DAILY_SUMMARY_CHAT_ID, report)


async def send_weekly_reports_to_team(bot: Bot, stores, as_of: date) -> None:
    """Weekly details belong only in the avitologists' chat, never in client chats."""
    summary_items: list[tuple[str, dict[str, int | float]]] = []
    details: list[str] = []
    for store in stores:
        try:
            text, stats = await build_weekly_report_with_summary(store, as_of)
            summary_items.append((store["store_name"], stats))
            details.append(text)
        except Exception as exc:
            logger.exception("Ошибка недельного отчёта для команды: %s", store["store_name"])
            await _notify_admins(
                bot,
                f"⚠️ Не собран weekly-отчёт для «{store['store_name']}»: {str(exc)[:800]}",
            )
    if summary_items:
        await send_weekly_team_summary(bot, summary_items, as_of)
    if details:
        await send_team_detailed_reports(bot, details)


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
    """Notify the client and team once per low-balance project event."""
    stores = await database.get_balance_monitored_stores()
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
            if config.LOW_BALANCE_ALERT_CHAT_ID:
                await _telegram_send_with_retry(
                    bot,
                    config.LOW_BALANCE_ALERT_CHAT_ID,
                    format_low_balance_team_alert(
                        [(store["store_name"], store["client_mention"], balance)]
                    ),
                )
            logger.warning("Отправлено предупреждение о низком балансе: %s", store["store_name"])
        except Exception as exc:
            logger.exception("Не удалось проверить баланс проекта %s", store["store_name"])
            await _notify_admins(
                bot, f"⚠️ Не проверен баланс «{store['store_name']}»: {str(exc)[:800]}"
            )

def local_today() -> date:
    from datetime import datetime

    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def is_last_day_of_month(day: date) -> bool:
    """Return true for the real final calendar day, including February."""
    return (day + timedelta(days=1)).month != day.month
