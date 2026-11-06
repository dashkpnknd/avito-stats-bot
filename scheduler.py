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
    format_daily_team_report,
    format_period_report,
    format_weekly_team_summary,
    report_period,
)

logger = logging.getLogger(__name__)

# APScheduler starts the daily and monthly jobs independently. Serialising them
# prevents the two report types from exhausting Avito's shared API quota at
# 10:00 on the first day of a month.
_REPORT_JOB_LOCK = asyncio.Lock()

# On Mondays the daily report has already read the exact two completed weeks
# needed for the team-only weekly view. Reusing this in-memory snapshot avoids
# a second burst of Avito calls immediately after the client delivery.
_WEEKLY_TEAM_SNAPSHOTS: dict[
    tuple[date, int],
    tuple[dict[str, int | float], dict[str, int | float], dict[str, float], datetime | None],
] = {}


async def _notify_admins(bot: Bot, text: str) -> None:
    for chat_id in config.ADMIN_ALERT_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            logger.exception("Не удалось уведомить администратора %s", chat_id)


def _parse_cached_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


async def get_tariff_valid_until(store, token: str) -> datetime | None:
    """Return a cached tariff renewal date, refreshing it only when needed.

    The regular account API exposes tariff operations, not a separate expiry
    field.  For Goods, the tariff period is a fixed 30 days, counted from its
    latest tariff operation.  ``tariff_verified`` remains useful diagnostic
    metadata but does not alter the published renewal date.
    """
    now = datetime.now(timezone.utc)
    cached_until = _parse_cached_datetime(store["tariff_valid_until"])
    if cached_until and cached_until > now:
        return cached_until

    checked_at = _parse_cached_datetime(store["tariff_checked_at"])
    if checked_at and checked_at + timedelta(hours=24) > now:
        return None

    try:
        operations = await avito_api.get_tariff_operations(token, now=now)
    except Exception:
        # Tariff information is supplementary.  A temporary error here must
        # never prevent the main statistics report from reaching the client.
        logger.exception("Не удалось прочитать историю тарифа для %s", store["store_name"])
        return None

    last_operation_at = operations[0] if operations else None
    verified = avito_api.has_confirmed_monthly_tariff_cycle(operations)
    valid_until = avito_api.add_tariff_period(last_operation_at) if last_operation_at else None
    await database.update_tariff_cache(
        store["id"],
        last_operation_at=last_operation_at.isoformat() if last_operation_at else None,
        valid_until=valid_until.isoformat() if valid_until else None,
        verified=verified,
    )
    return valid_until if valid_until and valid_until > now else None


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
    tariff_valid_until = await get_tariff_valid_until(store, token)
    if report_type == "daily":
        yesterday_stats = avito_api.sum_period(daily, yesterday, yesterday)
        week_stats = avito_api.sum_period(daily, week_start, week_end)
        previous_week_stats = avito_api.sum_period(daily, previous_week_start, previous_week_end)
        return format_daily_report(
            store["store_name"], yesterday, yesterday_stats, week_start, week_end,
            week_stats, previous_week_stats, balance,
            tariff_valid_until,
        )
    current = avito_api.sum_period(daily, current_from, current_to)
    previous = avito_api.sum_period(daily, previous_from, previous_to)
    return format_period_report(
        store["store_name"], report_type, current_from, current_to, current, previous,
        balance, tariff_valid_until,
    )


async def build_daily_report_with_summary(
    store, as_of: date
) -> tuple[str, str, dict[str, int | float]]:
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
    tariff_valid_until = await get_tariff_valid_until(store, token)
    yesterday_stats = avito_api.sum_period(daily, yesterday, yesterday)
    week_stats = avito_api.sum_period(daily, week_start, week_end)
    previous_week_stats = avito_api.sum_period(daily, previous_week_start, previous_week_end)
    client_report = format_daily_report(
        store["store_name"], yesterday, yesterday_stats, week_start, week_end,
        week_stats, previous_week_stats, balance, tariff_valid_until,
    )
    team_report = format_daily_team_report(
        store["store_name"], yesterday, yesterday_stats, balance, tariff_valid_until
    )
    _WEEKLY_TEAM_SNAPSHOTS[(as_of, int(store["id"]))] = (
        week_stats,
        previous_week_stats,
        balance,
        tariff_valid_until,
    )
    return client_report, team_report, yesterday_stats


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
    tariff_valid_until = await get_tariff_valid_until(store, token)
    current = avito_api.sum_period(daily, current_from, current_to)
    previous = avito_api.sum_period(daily, previous_from, previous_to)
    return (
        format_period_report(
            store["store_name"], "weekly", current_from, current_to, current, previous,
            balance, tariff_valid_until,
        ),
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
            text, team_text, yesterday_stats = await build_daily_report_with_summary(store, as_of)
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
            team_details.append(team_text if report_type == "daily" else text)
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
    current_from, current_to, _, _ = report_period("weekly", as_of)
    missing_snapshots = []
    for store in stores:
        snapshot = _WEEKLY_TEAM_SNAPSHOTS.pop((as_of, int(store["id"])), None)
        if snapshot:
            current, previous, balance, tariff_valid_until = snapshot
            summary_items.append((store["store_name"], current))
            details.append(
                format_period_report(
                    store["store_name"], "weekly", current_from, current_to,
                    current, previous, balance, tariff_valid_until,
                )
            )
            continue
        missing_snapshots.append(store)

    # A restart between daily and weekly jobs is unusual. In that case fall
    # back to a direct request, but report a single summary if Avito is still
    # throttling rather than emitting one noisy alert per project.
    failed_names: list[str] = []
    for store in missing_snapshots:
        try:
            text, stats = await build_weekly_report_with_summary(store, as_of)
            summary_items.append((store["store_name"], stats))
            details.append(text)
        except Exception:
            logger.exception("Ошибка недельного отчёта для команды: %s", store["store_name"])
            failed_names.append(store["store_name"])
    if failed_names:
        await _notify_admins(
            bot,
            "⚠️ Не собраны недельные отчёты: " + ", ".join(failed_names) + ".",
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
    """Deliver each low-balance cycle to client and team independently.

    A successful client send is stored separately from the team-chat send.
    Therefore a temporary failure in either destination is retried on the next
    hourly check without duplicating the destination that already received it.
    """
    stores = await database.get_balance_monitored_stores()
    failed_names: list[str] = []
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

            cycle_completed_at = store["low_balance_cycle_completed_at"]
            start_new_cycle = not bool(store["low_balance_is_low"]) or (
                bool(cycle_completed_at) and _is_reminder_due(cycle_completed_at)
            )
            if start_new_cycle:
                await database.start_low_balance_delivery_cycle(store["id"])
                client_sent_at = None
                team_sent_at = None
            else:
                client_sent_at = store["low_balance_client_sent_at"]
                team_sent_at = store["low_balance_team_sent_at"]

            # A fully completed cycle remains quiet until the 24-hour reminder
            # window elapses. A partially completed cycle is never skipped.
            if client_sent_at and (team_sent_at or not config.LOW_BALANCE_ALERT_CHAT_ID):
                continue

            if not client_sent_at:
                text = format_low_balance_client_alert(
                    store["store_name"], store["client_mention"], balance
                )
                await _telegram_send_with_retry(bot, store["chat_id"], text)
                await database.mark_low_balance_destination_sent(store["id"], "client")
                client_sent_at = "sent"

            if config.LOW_BALANCE_ALERT_CHAT_ID and not team_sent_at:
                await _telegram_send_with_retry(
                    bot,
                    config.LOW_BALANCE_ALERT_CHAT_ID,
                    format_low_balance_team_alert(
                        [(store["store_name"], store["client_mention"], balance)]
                    ),
                )
                await database.mark_low_balance_destination_sent(store["id"], "team")
                team_sent_at = "sent"

            if client_sent_at and (team_sent_at or not config.LOW_BALANCE_ALERT_CHAT_ID):
                await database.complete_low_balance_delivery_cycle(store["id"])
            logger.warning("Отправлено предупреждение о низком балансе: %s", store["store_name"])
        except Exception:
            logger.exception("Не удалось проверить баланс проекта %s", store["store_name"])
            failed_names.append(store["store_name"])
    if failed_names:
        await _notify_admins(
            bot, "⚠️ Не проверены балансы: " + ", ".join(failed_names) + ".")

def local_today() -> date:
    from datetime import datetime

    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def is_last_day_of_month(day: date) -> bool:
    """Return true for the real final calendar day, including February."""
    return (day + timedelta(days=1)).month != day.month
