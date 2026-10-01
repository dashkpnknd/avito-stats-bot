"""SQLite persistence and one-time migration from the first bot prototype."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

import config


def _cipher() -> Fernet:
    return Fernet(config.FERNET_KEY.encode())


def _encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        # Legacy databases stored the secret as text. It is re-encrypted when
        # the project is next saved, while remaining usable during migration.
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                store_name TEXT NOT NULL,
                client_id TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                user_id INTEGER,
                daily_enabled INTEGER NOT NULL DEFAULT 1,
                weekly_enabled INTEGER NOT NULL DEFAULT 1,
                monthly_enabled INTEGER NOT NULL DEFAULT 0,
                low_balance_enabled INTEGER NOT NULL DEFAULT 1,
                client_mention TEXT NOT NULL DEFAULT '',
                low_balance_is_low INTEGER NOT NULL DEFAULT 0,
                low_balance_last_alert_at TEXT,
                low_balance_client_sent_at TEXT,
                low_balance_team_sent_at TEXT,
                low_balance_cycle_completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = await _columns(db, "stores")
        for column, definition in {
            "daily_enabled": "INTEGER NOT NULL DEFAULT 1",
            "weekly_enabled": "INTEGER NOT NULL DEFAULT 1",
            "monthly_enabled": "INTEGER NOT NULL DEFAULT 0",
            "low_balance_enabled": "INTEGER NOT NULL DEFAULT 1",
            "client_mention": "TEXT NOT NULL DEFAULT ''",
            "low_balance_is_low": "INTEGER NOT NULL DEFAULT 0",
            "low_balance_last_alert_at": "TEXT",
            "low_balance_client_sent_at": "TEXT",
            "low_balance_team_sent_at": "TEXT",
            "low_balance_cycle_completed_at": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in columns:
                await db.execute(f"ALTER TABLE stores ADD COLUMN {column} {definition}")

        # Existing installations have one timestamp that meant a successful
        # alert cycle. Treat it as completed for both destinations so an
        # upgrade never produces an unexpected duplicate notification.
        await db.execute(
            """
            UPDATE stores
            SET low_balance_client_sent_at = COALESCE(low_balance_client_sent_at, low_balance_last_alert_at),
                low_balance_team_sent_at = COALESCE(low_balance_team_sent_at, low_balance_last_alert_at),
                low_balance_cycle_completed_at = COALESCE(low_balance_cycle_completed_at, low_balance_last_alert_at)
            WHERE low_balance_last_alert_at IS NOT NULL
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS report_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                report_type TEXT NOT NULL CHECK(report_type IN ('daily', 'weekly', 'monthly')),
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing', 'sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                telegram_message_id INTEGER,
                error_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(store_id, report_type, period_start, period_end)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_deliveries_status ON report_deliveries(status, updated_at)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS draft_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT NOT NULL UNIQUE,
                chat_invite_link TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                user_id INTEGER,
                client_mention TEXT NOT NULL DEFAULT '',
                daily_enabled INTEGER NOT NULL DEFAULT 0,
                weekly_enabled INTEGER NOT NULL DEFAULT 0,
                monthly_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = _now()
        await db.execute("UPDATE stores SET created_at = ? WHERE created_at = ''", (now,))
        await db.execute("UPDATE stores SET updated_at = ? WHERE updated_at = ''", (now,))
        # Migrate only the first prototype's plaintext client secrets. A token
        # that cannot be decrypted with this deployment key is legacy plaintext.
        async with db.execute("SELECT id, client_secret FROM stores") as cursor:
            legacy_secrets = await cursor.fetchall()
        for store_id, secret in legacy_secrets:
            try:
                _cipher().decrypt(secret.encode())
            except InvalidToken:
                await db.execute(
                    "UPDATE stores SET client_secret = ?, updated_at = ? WHERE id = ?",
                    (_encrypt(secret), now, store_id),
                )
        await db.commit()


async def add_store(
    chat_id: int,
    store_name: str,
    client_id: str,
    client_secret: str,
    user_id: int,
    client_mention: str = "",
) -> int:
    now = _now()
    async with aiosqlite.connect(config.DB_NAME) as db:
        cursor = await db.execute(
            """
            INSERT INTO stores (
                chat_id, store_name, client_id, client_secret, user_id,
                client_mention, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                store_name.strip(),
                client_id.strip(),
                _encrypt(client_secret.strip()),
                user_id,
                client_mention.strip(),
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def upsert_draft_project(
    store_name: str,
    chat_invite_link: str,
    client_id: str,
    client_secret: str,
    user_id: int,
    client_mention: str = "",
    *,
    daily_enabled: bool = False,
    weekly_enabled: bool = False,
    monthly_enabled: bool = False,
) -> int:
    """Securely stage a project until the bot is added to its client chat."""
    now = _now()
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM draft_projects WHERE store_name = ?", (store_name.strip(),)
        ) as cursor:
            existing = await cursor.fetchone()
        values = (
            chat_invite_link.strip(),
            client_id.strip(),
            _encrypt(client_secret.strip()),
            user_id,
            client_mention.strip(),
            int(daily_enabled),
            int(weekly_enabled),
            int(monthly_enabled),
            now,
        )
        if existing:
            await db.execute(
                """
                UPDATE draft_projects
                SET chat_invite_link = ?, client_id = ?, client_secret = ?, user_id = ?,
                    client_mention = ?, daily_enabled = ?, weekly_enabled = ?,
                    monthly_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, existing["id"]),
            )
            draft_id = existing["id"]
        else:
            cursor = await db.execute(
                """
                INSERT INTO draft_projects (
                    store_name, chat_invite_link, client_id, client_secret, user_id,
                    client_mention, daily_enabled, weekly_enabled, monthly_enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (store_name.strip(), *values, now),
            )
            draft_id = cursor.lastrowid
        await db.commit()
        return draft_id


async def get_all_draft_projects() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM draft_projects ORDER BY store_name COLLATE NOCASE"
        ) as cursor:
            return await cursor.fetchall()


async def activate_draft_project(store_name: str, chat_id: int) -> int:
    """Atomically move one validated draft into an active client-chat project."""
    now = _now()
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM draft_projects WHERE store_name = ?", (store_name,)
        ) as cursor:
            draft = await cursor.fetchone()
        if not draft:
            raise ValueError(f"Черновик «{store_name}» не найден")

        async with db.execute(
            "SELECT id FROM stores WHERE store_name = ? OR chat_id = ?",
            (store_name, chat_id),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            raise ValueError("Проект или чат уже подключён")

        cursor = await db.execute(
            """
            INSERT INTO stores (
                chat_id, store_name, client_id, client_secret, user_id,
                daily_enabled, weekly_enabled, monthly_enabled, low_balance_enabled,
                client_mention, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                chat_id, draft["store_name"], draft["client_id"],
                draft["client_secret"], draft["user_id"], draft["daily_enabled"],
                draft["weekly_enabled"], draft["monthly_enabled"],
                draft["client_mention"], now, now,
            ),
        )
        await db.execute("DELETE FROM draft_projects WHERE id = ?", (draft["id"],))
        await db.commit()
        return cursor.lastrowid


async def get_all_stores() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM stores ORDER BY store_name COLLATE NOCASE") as cursor:
            return await cursor.fetchall()


async def get_enabled_stores(report_type: str) -> list[aiosqlite.Row]:
    column = {"daily": "daily_enabled", "weekly": "weekly_enabled", "monthly": "monthly_enabled"}[report_type]
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM stores WHERE {column} = 1 ORDER BY store_name COLLATE NOCASE"
        ) as cursor:
            return await cursor.fetchall()


async def get_balance_monitored_stores() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM stores WHERE low_balance_enabled = 1 ORDER BY store_name COLLATE NOCASE"
        ) as cursor:
            return await cursor.fetchall()


async def get_store_by_id(store_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM stores WHERE id = ?", (store_id,)) as cursor:
            return await cursor.fetchone()


async def get_store_credentials(store: aiosqlite.Row) -> tuple[str, str]:
    return store["client_id"], _decrypt(store["client_secret"])


async def toggle_report(store_id: int, report_type: str) -> bool | None:
    column = {"daily": "daily_enabled", "weekly": "weekly_enabled", "monthly": "monthly_enabled"}[report_type]
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT {column} FROM stores WHERE id = ?", (store_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        enabled = not bool(row[column])
        await db.execute(
            f"UPDATE stores SET {column} = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _now(), store_id),
        )
        await db.commit()
        return enabled


async def toggle_low_balance_monitoring(store_id: int) -> bool | None:
    async with aiosqlite.connect(config.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT low_balance_enabled FROM stores WHERE id = ?", (store_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        enabled = not bool(row["low_balance_enabled"])
        await db.execute(
            "UPDATE stores SET low_balance_enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _now(), store_id),
        )
        await db.commit()
        return enabled


async def update_client_mention(store_id: int, client_mention: str) -> bool:
    async with aiosqlite.connect(config.DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE stores SET client_mention = ?, updated_at = ? WHERE id = ?",
            (client_mention.strip(), _now(), store_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_low_balance_state(
    store_id: int, *, is_low: bool, alert_sent: bool = False
) -> None:
    """Persist threshold state so the client is not messaged every poll."""
    async with aiosqlite.connect(config.DB_NAME) as db:
        if not is_low:
            await db.execute(
                """
                UPDATE stores
                SET low_balance_is_low = 0, low_balance_last_alert_at = NULL,
                    low_balance_client_sent_at = NULL, low_balance_team_sent_at = NULL,
                    low_balance_cycle_completed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_now(), store_id),
            )
        elif alert_sent:
            now = _now()
            await db.execute(
                """
                UPDATE stores
                SET low_balance_is_low = 1, low_balance_last_alert_at = ?,
                    low_balance_client_sent_at = ?, low_balance_team_sent_at = ?,
                    low_balance_cycle_completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, now, now, now, store_id),
            )
        else:
            await db.execute(
                "UPDATE stores SET low_balance_is_low = ?, updated_at = ? WHERE id = ?",
                (int(is_low), _now(), store_id),
            )
        await db.commit()


async def start_low_balance_delivery_cycle(store_id: int) -> None:
    """Start one two-destination notification cycle for an active low balance."""
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute(
            """
            UPDATE stores
            SET low_balance_is_low = 1, low_balance_client_sent_at = NULL,
                low_balance_team_sent_at = NULL, low_balance_cycle_completed_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (_now(), store_id),
        )
        await db.commit()


async def mark_low_balance_destination_sent(store_id: int, destination: str) -> None:
    """Persist one successful delivery without marking the other destination sent."""
    columns = {
        "client": "low_balance_client_sent_at",
        "team": "low_balance_team_sent_at",
    }
    try:
        column = columns[destination]
    except KeyError as exc:
        raise ValueError(f"Неизвестное назначение уведомления: {destination}") from exc
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute(
            f"UPDATE stores SET {column} = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), store_id),
        )
        await db.commit()


async def complete_low_balance_delivery_cycle(store_id: int) -> None:
    """Close a cycle only after both required destinations have succeeded."""
    now = _now()
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute(
            """
            UPDATE stores
            SET low_balance_last_alert_at = ?, low_balance_cycle_completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, now, store_id),
        )
        await db.commit()


async def delete_store(store_id: int) -> bool:
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute("DELETE FROM report_deliveries WHERE store_id = ?", (store_id,))
        cursor = await db.execute("DELETE FROM stores WHERE id = ?", (store_id,))
        await db.commit()
        return cursor.rowcount > 0


async def claim_delivery(store_id: int, report_type: str, period_start: str, period_end: str) -> int | None:
    """Atomically reserve a report. A sent period can never be sent again."""
    now = _now()
    async with aiosqlite.connect(config.DB_NAME) as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO report_deliveries (
                    store_id, report_type, period_start, period_end, status,
                    attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'processing', 1, ?, ?)
                """,
                (store_id, report_type, period_start, period_end, now, now),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            async with db.execute(
                """
                SELECT id, status FROM report_deliveries
                WHERE store_id = ? AND report_type = ? AND period_start = ? AND period_end = ?
                """,
                (store_id, report_type, period_start, period_end),
            ) as cursor:
                row = await cursor.fetchone()
            if not row or row[1] == "sent":
                return None
            await db.execute(
                """
                UPDATE report_deliveries
                SET status = 'processing', attempts = attempts + 1, error_text = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, row[0]),
            )
            await db.commit()
            return row[0]


async def mark_delivery_sent(delivery_id: int, message_id: int) -> None:
    now = _now()
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute(
            """
            UPDATE report_deliveries
            SET status = 'sent', telegram_message_id = ?, sent_at = ?, updated_at = ?, error_text = NULL
            WHERE id = ?
            """,
            (message_id, now, now, delivery_id),
        )
        await db.commit()


async def mark_delivery_failed(delivery_id: int, error_text: str) -> None:
    async with aiosqlite.connect(config.DB_NAME) as db:
        await db.execute(
            "UPDATE report_deliveries SET status = 'failed', error_text = ?, updated_at = ? WHERE id = ?",
            (error_text[:2000], _now(), delivery_id),
        )
        await db.commit()
