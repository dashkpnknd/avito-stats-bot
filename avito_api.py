"""Small, defensive client for the Avito account and item-statistics APIs."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiohttp

import config

TOKEN_URL = "https://api.avito.ru/token"
SELF_URL = "https://api.avito.ru/core/v1/accounts/self"
ITEMS_URL = "https://api.avito.ru/core/v1/items"
STATS_URL = "https://api.avito.ru/stats/v1/accounts/{user_id}/items"
STATS_V2_ITEMS_URL = "https://api.avito.ru/stats/v2/accounts/{user_id}/items"
BALANCE_URL = "https://api.avito.ru/core/v1/accounts/{user_id}/balance/"
# The CPA response contains a separately named ``advance`` in kopeks, unlike
# the regular wallet API which is returned in roubles.
CPA_BALANCE_URL = "https://api.avito.ru/cpa/v2/balanceInfo"
CALLS_STATS_URL = "https://api.avito.ru/core/v1/accounts/{user_id}/calls/stats/"
SPENDINGS_URL = "https://api.avito.ru/stats/v2/accounts/{user_id}/spendings"
# This is the regular account endpoint, not the Avito Promo agency API.  It
# accepts the same client-credentials token used for a store's reports.
OPERATIONS_HISTORY_URL = "https://api.avito.ru/core/v1/accounts/operations_history/"


class AvitoAPIError(RuntimeError):
    pass


class AvitoRateLimitError(AvitoAPIError):
    """Avito temporarily throttled this API application."""


def _response_error(scope: str, status: int, body: str) -> AvitoAPIError:
    error_class = AvitoRateLimitError if status == 429 else AvitoAPIError
    return error_class(f"{scope}: HTTP {status}: {body[:400]}")


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _metric(day: dict[str, Any], *names: str) -> float:
    for name in names:
        if name in day:
            return _number(day[name])
    return 0.0


async def _request_with_retry(call, attempts: int = 4):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except (aiohttp.ClientError, asyncio.TimeoutError, AvitoAPIError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                # Avito's 429 is a temporary quota response, not a malformed
                # request. Give the shared API quota time to recover.
                delay = 20 * (2**attempt) if isinstance(exc, AvitoRateLimitError) else 2**attempt
                await asyncio.sleep(delay)
    if last_error is None:
        raise AvitoAPIError("Неизвестная ошибка Avito API")
    # aiohttp's TimeoutError has an empty string representation. Keep the
    # class name, otherwise operational alerts end with a useless colon.
    detail = str(last_error).strip() or type(last_error).__name__
    raise AvitoAPIError(detail)


async def get_avito_token(client_id: str, client_secret: str) -> str:
    async def request() -> str:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id.strip(),
                    "client_secret": client_secret.strip(),
                },
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise _response_error("Токен Avito", response.status, body)
                token = (await response.json()).get("access_token")
                if not token:
                    raise AvitoAPIError("Avito не вернул access_token")
                return token

    return await _request_with_retry(request)


async def get_avito_user_id(token: str) -> int:
    async def request() -> int:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(SELF_URL, headers={"Authorization": f"Bearer {token}"}) as response:
                body = await response.text()
                if response.status != 200:
                    raise _response_error("Аккаунт Avito", response.status, body)
                user_id = (await response.json()).get("id")
                if not user_id:
                    raise AvitoAPIError("Avito не вернул ID аккаунта")
                return int(user_id)

    return await _request_with_retry(request)


def _balance_value(payload: dict[str, Any], *names: str) -> float:
    """Read Avito balance fields while tolerating documented field aliases."""
    for name in names:
        if name in payload:
            return _number(payload[name])
    balance = payload.get("balance")
    if isinstance(balance, dict):
        for name in names:
            if name in balance:
                return _number(balance[name])
    return 0.0


def _cpa_balance_value(payload: dict[str, Any]) -> float:
    """Read the current CPA wallet balance and convert kopeks to roubles."""
    result = payload.get("result") or payload
    return _number(result.get("balance")) / 100


async def get_balance(token: str, user_id: int) -> dict[str, float]:
    """Return the Avito wallet, CPA balance and their total.

    ``/core/.../balance`` exposes the actual wallet. The CPA endpoint has two
    values: ``balance`` and ``advance``. The current CPA balance is the
    second available account balance shown to operators as «Аванс»; the
    ``advance`` field is a separate technical/credit parameter. CPA values
    are returned in kopeks.
    """
    async def request() -> dict[str, float]:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                BALANCE_URL.format(user_id=user_id),
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise _response_error("Баланс Avito", response.status, body)
                payload = await response.json()
                wallet = _balance_value(payload, "real", "wallet", "balance")
                async with session.post(
                    CPA_BALANCE_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json={},
                ) as cpa_response:
                    cpa_body = await cpa_response.text()
                    if cpa_response.status != 200:
                        raise _response_error("Аванс Avito", cpa_response.status, cpa_body)
                    cpa_payload = await cpa_response.json()
                advance = _cpa_balance_value(cpa_payload)
                return {
                    "wallet": round(wallet, 2),
                    "advance": round(advance, 2),
                    "total": round(wallet + advance, 2),
                }

    return await _request_with_retry(request)


def _parse_api_datetime(value: Any) -> datetime | None:
    """Parse an Avito RFC3339 timestamp without treating a bad row as fatal."""
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def add_tariff_period(value: datetime) -> datetime:
    """Return the end of Avito's fixed 30-day subscription period.

    The regular Goods tariff renews after 30 days from its tariff operation;
    it is not a calendar-month subscription.  The time and timezone are kept
    exactly so the displayed date agrees with the account's next debit.
    """
    return value + timedelta(days=30)


async def get_latest_tariff_operation(
    token: str,
    *,
    now: datetime | None = None,
    lookback_days: int = 42,
) -> datetime | None:
    """Find the most recent subscription/tariff operation for one account.

    ``operations_history`` is limited by Avito to a seven-day request window.
    We only inspect a short rolling period, then the caller caches the result
    until the calculated renewal date.  This avoids a daily historical scan.
    ``updatedAt`` is the documented operation timestamp; ``paidAt`` is used as
    a fallback for payload variants that expose only the payment timestamp.
    """
    if lookback_days < 1:
        raise ValueError("lookback_days должен быть положительным")

    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start_limit = end - timedelta(days=lookback_days)
    latest: datetime | None = None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while end > start_limit:
            # The public contract permits a period of at most one week.
            start = max(start_limit, end - timedelta(days=6, hours=23, minutes=59))

            async def request() -> dict[str, Any]:
                async with session.post(
                    OPERATIONS_HISTORY_URL,
                    headers=headers,
                    json={"dateTimeFrom": start.isoformat(), "dateTimeTo": end.isoformat()},
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise _response_error("История операций Avito", response.status, body)
                    return await response.json()

            payload = await _request_with_retry(request)
            operations = ((payload.get("result") or {}).get("operations") or [])
            for operation in operations:
                if operation.get("serviceType") not in {"tariff", "subscription"}:
                    continue
                occurred_at = _parse_api_datetime(
                    operation.get("updatedAt") or operation.get("paidAt")
                )
                if occurred_at and (latest is None or occurred_at > latest):
                    latest = occurred_at
            end = start - timedelta(seconds=1)

    return latest


async def get_tariff_operations(
    token: str,
    *,
    now: datetime | None = None,
    lookback_days: int = 92,
) -> list[datetime]:
    """Return tariff/subscription operation timestamps, newest first.

    Unlike :func:`get_latest_tariff_operation`, this is used only to validate
    that the most recent tariff debit is part of a monthly cycle.  The result
    lets the caller avoid presenting a forecast as a confirmed expiry date.
    """
    if lookback_days < 1:
        raise ValueError("lookback_days должен быть положительным")

    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start_limit = end - timedelta(days=lookback_days)
    operations_at: set[datetime] = set()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while end > start_limit:
            start = max(start_limit, end - timedelta(days=6, hours=23, minutes=59))

            async def request() -> dict[str, Any]:
                async with session.post(
                    OPERATIONS_HISTORY_URL,
                    headers=headers,
                    json={"dateTimeFrom": start.isoformat(), "dateTimeTo": end.isoformat()},
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise _response_error("История операций Avito", response.status, body)
                    return await response.json()

            payload = await _request_with_retry(request)
            for operation in ((payload.get("result") or {}).get("operations") or []):
                if operation.get("serviceType") not in {"tariff", "subscription"}:
                    continue
                occurred_at = _parse_api_datetime(
                    operation.get("updatedAt") or operation.get("paidAt")
                )
                if occurred_at:
                    operations_at.add(occurred_at)
            end = start - timedelta(seconds=1)

    return sorted(operations_at, reverse=True)


def has_confirmed_monthly_tariff_cycle(operations: list[datetime]) -> bool:
    """Whether the two latest tariff debits occurred on consecutive months.

    The account API does not provide an explicit expiry field for ordinary
    Goods accounts.  We therefore display a calculated date only after seeing
    the latest debit repeat on the same calendar day in the preceding month.
    A different date can indicate a one-off tariff operation and is not safe
    to present as an active-until date.
    """
    if len(operations) < 2:
        return False
    latest, previous = sorted(operations, reverse=True)[:2]
    return add_tariff_period(previous) == latest


async def get_all_item_ids(token: str) -> list[int]:
    """Read all configured item states; pagination has no arbitrary 1,900-item cap."""
    headers = {"Authorization": f"Bearer {token}"}
    item_ids: set[int] = set()
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for status in config.AVITO_ITEM_STATUSES:
            page = 1
            while True:
                async def request() -> dict[str, Any]:
                    async with session.get(
                        ITEMS_URL,
                        headers=headers,
                        params={"per_page": 100, "page": page, "status": status},
                    ) as response:
                        body = await response.text()
                        if response.status != 200:
                            raise _response_error(
                                f"Список объявлений ({status})", response.status, body
                            )
                        return await response.json()

                payload = await _request_with_retry(request)
                items = payload.get("resources") or []
                item_ids.update(int(item["id"]) for item in items if item.get("id"))
                if len(items) < 100:
                    break
                page += 1

    return sorted(item_ids)


async def get_daily_stats(
    token: str, user_id: int, date_from: date, date_to: date
) -> dict[date, dict[str, float]]:
    """Return daily sums for an inclusive period across all selected adverts."""
    item_ids = await get_all_item_ids(token)
    if not item_ids:
        return {}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    daily: dict[date, dict[str, float]] = defaultdict(
        lambda: {"views": 0.0, "contacts": 0.0, "favorites": 0.0, "calls": 0.0, "messages": 0.0, "spend": 0.0}
    )
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for start in range(0, len(item_ids), 200):
            item_chunk = item_ids[start : start + 200]

            async def request() -> dict[str, Any]:
                async with session.post(
                    STATS_URL.format(user_id=user_id),
                    headers=headers,
                    json={
                        "dateFrom": date_from.isoformat(),
                        "dateTo": date_to.isoformat(),
                        "itemIds": item_chunk,
                        "fields": list(config.AVITO_STATS_FIELDS),
                    },
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise _response_error("Статистика Avito", response.status, body)
                    return await response.json()

            payload = await _request_with_retry(request)
            for item in (payload.get("result") or {}).get("items") or []:
                for row in item.get("stats") or []:
                    try:
                        row_date = date.fromisoformat(row["date"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not date_from <= row_date <= date_to:
                        continue
                    totals = daily[row_date]
                    totals["views"] += _metric(row, "uniqViews", "views")
                    totals["contacts"] += _metric(row, "uniqContacts", "contacts")
                    # The optional aliases make the formatter future-proof but
                    # are not requested until the Avito API grants the fields.
                    totals["favorites"] += _metric(row, "favorites", "uniqFavorites")
                    totals["calls"] += _metric(row, "calls", "phoneContacts")
                    totals["messages"] += _metric(row, "messages", "chatContacts")
                    totals["spend"] += _metric(row, "spend", "expenses")
    return dict(daily)


async def get_daily_promo_stats(
    token: str, user_id: int, date_from: date, date_to: date
) -> dict[date, dict[str, float]]:
    """Return daily views, contacts and real messenger contacts from Promo v2.

    The v1 item-statistics endpoint has no messages metric.  The Promo v2
    endpoint exposes ``contactsMessenger`` per day, so it must be used rather
    than deriving messages from all contacts or filling a zero placeholder.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def request() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                STATS_V2_ITEMS_URL.format(user_id=user_id),
                headers=headers,
                json={
                    "dateFrom": date_from.isoformat(),
                    "dateTo": date_to.isoformat(),
                    "grouping": "day",
                    "metrics": ["views", "contacts", "contactsMessenger"],
                },
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise _response_error("Сообщения Avito", response.status, body)
                return await response.json()

    payload = await _request_with_retry(request)
    daily: dict[date, dict[str, float]] = defaultdict(
        lambda: {"views": 0.0, "contacts": 0.0, "favorites": 0.0,
                 "calls": 0.0, "messages": 0.0, "spend": 0.0}
    )
    metric_names = {
        "views": "views",
        "contacts": "contacts",
        "contactsMessenger": "messages",
    }
    for grouping in (payload.get("result") or {}).get("groupings") or []:
        try:
            # Promo v2 returns a UTC Unix timestamp as the daily grouping id.
            grouping_date = datetime.fromtimestamp(int(grouping["id"]), tz=timezone.utc).date()
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
        if not date_from <= grouping_date <= date_to:
            continue
        totals = daily[grouping_date]
        for metric in grouping.get("metrics") or []:
            key = metric_names.get(metric.get("slug"))
            if key:
                totals[key] = _number(metric.get("value"))
    return dict(daily)


async def get_daily_calls(
    token: str, user_id: int, item_ids: list[int], date_from: date, date_to: date
) -> dict[date, int]:
    """Return calls per day from Avito's dedicated call-statistics endpoint."""
    if not item_ids:
        return {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    calls: dict[date, int] = defaultdict(int)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for start in range(0, len(item_ids), 100):
            item_chunk = item_ids[start : start + 100]

            async def request() -> dict[str, Any]:
                async with session.post(
                    CALLS_STATS_URL.format(user_id=user_id),
                    headers=headers,
                    json={
                        "dateFrom": date_from.isoformat(),
                        "dateTo": date_to.isoformat(),
                        "itemIds": item_chunk,
                    },
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise _response_error("Звонки Avito", response.status, body)
                    return await response.json()

            payload = await _request_with_retry(request)
            for item in (payload.get("result") or {}).get("items") or []:
                for row in item.get("days") or []:
                    try:
                        row_date = date.fromisoformat(row["date"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if date_from <= row_date <= date_to:
                        calls[row_date] += int(_number(row.get("calls")))
    return dict(calls)


async def get_daily_spendings(
    token: str, user_id: int, date_from: date, date_to: date
) -> dict[date, float]:
    """Return all account expenses grouped by day, in roubles."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def request() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                SPENDINGS_URL.format(user_id=user_id),
                headers=headers,
                json={
                    "dateFrom": date_from.isoformat(),
                    "dateTo": date_to.isoformat(),
                    "grouping": "day",
                    "spendingTypes": ["all"],
                },
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise _response_error("Расходы Avito", response.status, body)
                return await response.json()

    payload = await _request_with_retry(request)
    result: dict[date, float] = {}
    for grouping in (payload.get("result") or {}).get("groupings") or []:
        try:
            grouping_date = date.fromisoformat(grouping["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if not date_from <= grouping_date <= date_to:
            continue
        result[grouping_date] = round(
            sum(_number(item.get("value")) for item in grouping.get("spendings") or []), 2
        )
    return result


def sum_period(daily: dict[date, dict[str, float]], date_from: date, date_to: date) -> dict[str, int | float]:
    result: dict[str, float] = {
        "views": 0.0,
        "contacts": 0.0,
        "favorites": 0.0,
        "calls": 0.0,
        "messages": 0.0,
        "spend": 0.0,
    }
    cursor = date_from
    while cursor <= date_to:
        row = daily.get(cursor, {})
        for key in result:
            result[key] += _number(row.get(key))
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return {
        key: round(value, 2) if key == "spend" else int(value)
        for key, value in result.items()
    }
