"""Small, defensive client for the Avito account and item-statistics APIs."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

import aiohttp

import config

TOKEN_URL = "https://api.avito.ru/token"
SELF_URL = "https://api.avito.ru/core/v1/accounts/self"
ITEMS_URL = "https://api.avito.ru/core/v1/items"
STATS_URL = "https://api.avito.ru/stats/v1/accounts/{user_id}/items"
STATS_V2_ITEMS_URL = "https://api.avito.ru/stats/v2/accounts/{user_id}/items"
BALANCE_URL = "https://api.avito.ru/core/v1/accounts/{user_id}/balance/"
# This is the Avito balance displayed as «Аванс» in the connected account's
# interface. The CPA response is in kopeks, unlike the regular wallet API.
CPA_BALANCE_URL = "https://api.avito.ru/cpa/v2/balanceInfo"
CALLS_STATS_URL = "https://api.avito.ru/core/v1/accounts/{user_id}/calls/stats/"
SPENDINGS_URL = "https://api.avito.ru/stats/v2/accounts/{user_id}/spendings"


class AvitoAPIError(RuntimeError):
    pass


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


async def _request_with_retry(call, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except (aiohttp.ClientError, asyncio.TimeoutError, AvitoAPIError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(2**attempt)
    raise AvitoAPIError(str(last_error) if last_error else "Неизвестная ошибка Avito API")


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
                    raise AvitoAPIError(f"Токен Avito: HTTP {response.status}: {body[:400]}")
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
                    raise AvitoAPIError(f"Аккаунт Avito: HTTP {response.status}: {body[:400]}")
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


async def get_balance(token: str, user_id: int) -> dict[str, float]:
    """Return the Avito wallet, CPA advance balance and their total.

    ``/core/.../balance`` returns the real wallet and *bonus* funds.  It does
    not expose the value named «Аванс» in the Avito UI. That value is the
    current CPA balance returned by ``/cpa/v2/balanceInfo`` in kopeks.
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
                    raise AvitoAPIError(f"Баланс Avito: HTTP {response.status}: {body[:400]}")
                payload = await response.json()
                wallet = _balance_value(payload, "real", "wallet", "balance")
                async with session.post(
                    CPA_BALANCE_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json={},
                ) as cpa_response:
                    cpa_body = await cpa_response.text()
                    if cpa_response.status != 200:
                        raise AvitoAPIError(
                            f"Аванс Avito: HTTP {cpa_response.status}: {cpa_body[:400]}"
                        )
                    cpa_payload = await cpa_response.json()
                cpa_result = cpa_payload.get("result") or cpa_payload
                # The documented field ``balance`` is the current CPA balance
                # (the UI's «Аванс»); divide kopeks by 100 into roubles.
                advance = _number(cpa_result.get("balance")) / 100
                return {
                    "wallet": round(wallet, 2),
                    "advance": round(advance, 2),
                    "total": round(wallet + advance, 2),
                }

    return await _request_with_retry(request)


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
                            raise AvitoAPIError(
                                f"Список объявлений ({status}): HTTP {response.status}: {body[:400]}"
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
                        raise AvitoAPIError(f"Статистика Avito: HTTP {response.status}: {body[:400]}")
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
                    raise AvitoAPIError(
                        f"Сообщения Avito: HTTP {response.status}: {body[:400]}"
                    )
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
                        raise AvitoAPIError(f"Звонки Avito: HTTP {response.status}: {body[:400]}")
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
                    raise AvitoAPIError(f"Расходы Avito: HTTP {response.status}: {body[:400]}")
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
