"""Report periods and client-facing Telegram message templates."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape


REPORT_LABELS = {"weekly": "Еженедельный", "monthly": "Ежемесячный"}


def report_period(report_type: str, as_of: date) -> tuple[date, date, date, date]:
    if report_type == "daily":
        end = as_of - timedelta(days=1)
        return end, end, end - timedelta(days=1), end - timedelta(days=1)
    if report_type == "weekly":
        end = as_of - timedelta(days=as_of.weekday() + 1)
        start = end - timedelta(days=6)
        return start, end, start - timedelta(days=7), end - timedelta(days=7)
    if report_type == "monthly":
        first_this_month = as_of.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
        previous_end = start - timedelta(days=1)
        return start, end, previous_end.replace(day=1), previous_end
    raise ValueError(f"Неизвестный тип отчёта: {report_type}")


def daily_report_periods(as_of: date) -> tuple[date, date, date, date, date]:
    yesterday = as_of - timedelta(days=1)
    week_start = yesterday - timedelta(days=6)
    return yesterday, week_start, yesterday, week_start - timedelta(days=7), week_start - timedelta(days=1)


def _range_label(date_from: date, date_to: date) -> str:
    return date_from.strftime("%d.%m") if date_from == date_to else f"{date_from:%d.%m} – {date_to:%d.%m}"


def _money(value: int | float, decimals: int = 0) -> str:
    value = float(value)
    if decimals == 0 and value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def _change(current: int | float, previous: int | float) -> str:
    if previous == 0:
        return "—" if current == 0 else "▲ новый"
    value = (float(current) - float(previous)) / float(previous) * 100
    number = f"{abs(value):.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"▲ +{number}%" if value > 0 else (f"▼ -{number}%" if value < 0 else "— 0%")


def _metric_line(label: str, value: int | float, previous: int | float | None = None) -> str:
    value_text = _money(value)
    return f"{label}: {value_text}" if previous is None else f"{label}: {value_text}   {_change(value, previous)}"


def _period_block(
    title: str,
    date_from: date,
    date_to: date,
    stats: dict[str, int | float],
    previous: dict[str, int | float] | None = None,
) -> list[str]:
    lines = [f"📅 <b>{title}</b> ({_range_label(date_from, date_to)})"]
    lines.append(_metric_line("👁 Просмотры", stats["views"], previous["views"] if previous else None))
    lines.append(_metric_line("📞 Контакты", stats["contacts"], previous["contacts"] if previous else None))
    lines.append(f"   ├ Звонки: {_money(stats.get('calls', 0))}")
    lines.append(f"   └ Сообщения: {_money(stats.get('messages', 0))}")
    # Keep the rouble sign beside the amount, before the comparison marker:
    # ``10 909 ₽   ▲ +1,3%`` is easier to scan in a client chat.
    spend_line = f"💰 Расходы: {_money(stats.get('spend', 0))} ₽"
    if previous is not None:
        spend_line += f"   {_change(stats.get('spend', 0), previous.get('spend', 0))}"
    lines.append(spend_line)
    views, contacts, spend = float(stats["views"]), float(stats["contacts"]), float(stats.get("spend", 0))
    lines.append(f"📊 Цена/просмотр: {_money(spend / views if views else 0, 2)} ₽")
    lines.append(f"📊 Цена/контакт: {_money(spend / contacts if contacts else 0, 2)} ₽")
    return lines


def format_daily_report(
    store_name: str,
    yesterday: date,
    yesterday_stats: dict[str, int | float],
    week_start: date,
    week_end: date,
    week_stats: dict[str, int | float],
    previous_week_stats: dict[str, int | float],
    balance: dict[str, float],
) -> str:
    lines = [f"📊 <b>Статистика: {escape(store_name.upper())}</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    lines.extend(_period_block("Вчера", yesterday, yesterday, yesterday_stats))
    lines.append("")
    lines.extend(_period_block("Неделя", week_start, week_end, week_stats, previous_week_stats))
    lines.extend(["", f"💳 Кошелёк: <b>{_money(balance['wallet'])} ₽</b> | Аванс: <b>{_money(balance['advance'])} ₽</b>"])
    return "\n".join(lines)


def format_period_report(
    store_name: str,
    report_type: str,
    current_from: date,
    current_to: date,
    current: dict[str, int | float],
    previous: dict[str, int | float],
    balance: dict[str, float],
) -> str:
    lines = [f"📊 <b>Статистика: {escape(store_name.upper())}</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    lines.extend(_period_block(REPORT_LABELS[report_type], current_from, current_to, current, previous))
    lines.extend(["", f"💳 Кошелёк: <b>{_money(balance['wallet'])} ₽</b> | Аванс: <b>{_money(balance['advance'])} ₽</b>"])
    return "\n".join(lines)


def format_low_balance_client_alert(store_name: str, mention: str, balance: dict[str, float]) -> str:
    # The recipient is already in the project's chat, so repeating its name
    # is noise. Keep the personal tag with the action question at the end.
    question = "Сегодня получится пополнить?"
    if mention.strip():
        question += f" {escape(mention.strip())}"
    return "\n".join(
        [
            "❗ <b>Низкий баланс Avito</b>",
            "",
            f"💳 Кошелёк: <b>{balance['wallet']:.2f} ₽</b>",
            f"💳 Аванс: <b>{balance['advance']:.2f} ₽</b>",
            f"💳 Общий остаток: <b>{balance['total']:.2f} ₽</b>",
            "",
            "Нужно пополнить баланс, чтобы объявления не пропали из поиска.",
            question,
        ]
    )


def format_low_balance_team_alert(items: list[tuple[str, str, dict[str, float]]]) -> str:
    lines = ["❗ <b>Низкие балансы Avito</b>", ""]
    for store_name, mention, balance in items:
        suffix = f" — {escape(mention)}" if mention else ""
        lines.extend([f"<b>{escape(store_name)}</b>{suffix}", f"Кошелёк: {balance['wallet']:.2f} ₽ | Аванс: {balance['advance']:.2f} ₽ | <b>Итого: {balance['total']:.2f} ₽</b>"])
    return "\n".join(lines)
