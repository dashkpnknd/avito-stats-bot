"""Report periods and a single client-facing message template."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape


REPORT_LABELS = {"daily": "Ежедневный", "weekly": "Еженедельный", "monthly": "Ежемесячный"}


def report_period(report_type: str, as_of: date) -> tuple[date, date, date, date]:
    """Return current and previous fully completed, equivalent periods."""
    if report_type == "daily":
        end = as_of - timedelta(days=1)
        start = end
        return start, end, start - timedelta(days=1), start - timedelta(days=1)
    if report_type == "weekly":
        # A Monday run reports the preceding Monday–Sunday week.
        end = as_of - timedelta(days=as_of.weekday() + 1)
        start = end - timedelta(days=6)
        return start, end, start - timedelta(days=7), end - timedelta(days=7)
    if report_type == "monthly":
        first_this_month = as_of.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
        previous_end = start - timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        return start, end, previous_start, previous_end
    raise ValueError(f"Неизвестный тип отчёта: {report_type}")


def _range_label(date_from: date, date_to: date) -> str:
    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")
    return f"{date_from.strftime('%d.%m.%Y')}–{date_to.strftime('%d.%m.%Y')}"


def _change(current: int | float, previous: int | float) -> str:
    if previous == 0:
        return "—" if current == 0 else "новый результат"
    change = (current - previous) / previous * 100
    sign = "+" if change > 0 else ""
    return f"{sign}{change:.0f}%"


def format_report(
    store_name: str,
    report_type: str,
    current_from: date,
    current_to: date,
    current: dict[str, int | float],
    previous: dict[str, int | float],
    balance: dict[str, float] | None = None,
) -> str:
    """Build only metrics received from Avito; no invented zero-value sections."""
    lines = [
        f"📈 <b>{REPORT_LABELS[report_type]} отчёт Avito</b>",
        f"<b>{escape(store_name)}</b>",
        f"Период: {_range_label(current_from, current_to)}",
        "",
        f"👁 Просмотры: <b>{current['views']}</b> ({_change(current['views'], previous['views'])})",
        f"📞 Контакты: <b>{current['contacts']}</b> ({_change(current['contacts'], previous['contacts'])})",
    ]
    if current.get("favorites") or previous.get("favorites"):
        lines.append(f"⭐ Добавили в избранное: <b>{current['favorites']}</b> ({_change(current['favorites'], previous['favorites'])})")
    if current.get("calls") or previous.get("calls"):
        lines.append(f"└ Звонки: <b>{current['calls']}</b>")
    if current.get("messages") or previous.get("messages"):
        lines.append(f"└ Сообщения: <b>{current['messages']}</b>")
    if current.get("spend") or previous.get("spend"):
        spend = float(current["spend"])
        lines.append(f"💰 Расходы: <b>{spend:.2f} ₽</b>")
        if current["views"]:
            lines.append(f"Цена просмотра: <b>{spend / current['views']:.2f} ₽</b>")
        if current["contacts"]:
            lines.append(f"Цена контакта: <b>{spend / current['contacts']:.2f} ₽</b>")
    if balance is not None:
        lines.extend(
            [
                "",
                "💳 <b>Бюджет на текущий момент</b>",
                f"Кошелёк: <b>{balance['wallet']:.2f} ₽</b>",
                f"Аванс: <b>{balance['advance']:.2f} ₽</b>",
                f"Всего: <b>{balance['total']:.2f} ₽</b>",
            ]
        )
    return "\n".join(lines)


def format_low_balance_client_alert(store_name: str, mention: str, balance: dict[str, float]) -> str:
    greeting = f"{escape(mention.strip())}\n" if mention.strip() else ""
    return "\n".join(
        [
            "❗ <b>Низкий баланс Avito</b>",
            greeting.rstrip(),
            f"Проект: <b>{escape(store_name)}</b>",
            f"Кошелёк: <b>{balance['wallet']:.2f} ₽</b>",
            f"Аванс: <b>{balance['advance']:.2f} ₽</b>",
            f"Общий остаток: <b>{balance['total']:.2f} ₽</b>",
            "",
            "Нужно пополнить баланс, чтобы объявления не пропали из поиска.",
            "Сегодня получится пополнить?",
        ]
    )


def format_low_balance_team_alert(items: list[tuple[str, str, dict[str, float]]]) -> str:
    lines = ["❗ <b>Низкие балансы Avito</b>", ""]
    for store_name, mention, balance in items:
        suffix = f" — {escape(mention)}" if mention else ""
        lines.extend(
            [
                f"<b>{escape(store_name)}</b>{suffix}",
                f"Кошелёк: {balance['wallet']:.2f} ₽ | Аванс: {balance['advance']:.2f} ₽ | <b>Итого: {balance['total']:.2f} ₽</b>",
            ]
        )
    return "\n".join(lines)
