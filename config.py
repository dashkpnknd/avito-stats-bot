"""Runtime configuration. Secrets must only be passed through environment variables."""

import logging
import os
from pathlib import Path


def _int_set(value: str) -> set[int]:
    result = set()
    for part in value.split(","):
        part = part.strip()
        if part:
            result.add(int(part))
    return result


def _username_set(value: str) -> set[str]:
    """Normalize Telegram @usernames for the access-control allowlist."""
    return {
        part.strip().lstrip("@").casefold()
        for part in value.split(",")
        if part.strip().lstrip("@")
    }


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _int_set(os.getenv("ADMIN_IDS", ""))
ADMIN_USERNAMES = _username_set(os.getenv("ADMIN_USERNAMES", ""))
ADMIN_ALERT_CHAT_IDS = _int_set(os.getenv("ADMIN_ALERT_CHAT_IDS", "")) or ADMIN_IDS
DB_NAME = os.getenv("DB_NAME", "data/avito_stats.sqlite3")
FERNET_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

DAILY_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "10"))
DAILY_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
WEEKLY_HOUR = int(os.getenv("WEEKLY_REPORT_HOUR", "10"))
WEEKLY_MINUTE = int(os.getenv("WEEKLY_REPORT_MINUTE", "0"))
MONTHLY_HOUR = int(os.getenv("MONTHLY_REPORT_HOUR", "10"))
MONTHLY_MINUTE = int(os.getenv("MONTHLY_REPORT_MINUTE", "0"))

# Current balance monitoring. The balance is the sum of Avito's wallet and
# advance balances. A project may disable monitoring in the bot panel.
LOW_BALANCE_THRESHOLD = float(os.getenv("LOW_BALANCE_THRESHOLD", "3000"))
LOW_BALANCE_CHECK_MINUTES = int(os.getenv("LOW_BALANCE_CHECK_MINUTES", "60"))
LOW_BALANCE_REMINDER_HOURS = int(os.getenv("LOW_BALANCE_REMINDER_HOURS", "24"))
# A dedicated chat/channel for one aggregated notification about all accounts
# that need attention. The bot must be added there and allowed to post.
LOW_BALANCE_ALERT_CHAT_ID = int(os.getenv("LOW_BALANCE_ALERT_CHAT_ID", "0") or 0)

# These are the two documented and currently used item-statistics fields. More
# fields can be enabled only after they are confirmed for the connected Avito API.
AVITO_STATS_FIELDS = tuple(
    x.strip() for x in os.getenv("AVITO_STATS_FIELDS", "uniqViews,uniqContacts").split(",") if x.strip()
)
AVITO_ITEM_STATUSES = tuple(
    x.strip() for x in os.getenv("AVITO_ITEM_STATUSES", "active,old").split(",") if x.strip()
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not FERNET_KEY:
        missing.append("CREDENTIALS_ENCRYPTION_KEY")
    if not ADMIN_IDS and not ADMIN_USERNAMES:
        missing.append("ADMIN_IDS or ADMIN_USERNAMES")
    if missing:
        raise RuntimeError("Не заданы обязательные переменные окружения: " + ", ".join(missing))

    for value in (DAILY_HOUR, WEEKLY_HOUR, MONTHLY_HOUR):
        if not 0 <= value <= 23:
            raise RuntimeError("Час отправки должен быть от 0 до 23")
    for value in (DAILY_MINUTE, WEEKLY_MINUTE, MONTHLY_MINUTE):
        if not 0 <= value <= 59:
            raise RuntimeError("Минута отправки должна быть от 0 до 59")
    if LOW_BALANCE_THRESHOLD < 0:
        raise RuntimeError("Порог низкого баланса не может быть отрицательным")
    if LOW_BALANCE_CHECK_MINUTES < 5:
        raise RuntimeError("Проверка баланса должна выполняться не реже чем раз в 5 минут")
    if LOW_BALANCE_REMINDER_HOURS < 1:
        raise RuntimeError("Интервал повторного напоминания должен быть не меньше часа")

    Path(DB_NAME).parent.mkdir(parents=True, exist_ok=True)
