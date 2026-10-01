import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DB_NAME"] = str(Path(_TEMP_DIR.name) / "test.sqlite3")
os.environ["CREDENTIALS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import database  # noqa: E402


class DatabaseTests(unittest.TestCase):
    def test_project_defaults_and_low_balance_state(self):
        async def scenario():
            await database.init_db()
            store_id = await database.add_store(123, "Test", "id", "secret", 321, "@client")
            store = await database.get_store_by_id(store_id)
            self.assertEqual(store["daily_enabled"], 1)
            self.assertEqual(store["weekly_enabled"], 1)
            self.assertEqual(store["monthly_enabled"], 0)
            self.assertEqual(store["low_balance_enabled"], 1)
            self.assertEqual(store["client_mention"], "@client")
            self.assertEqual(await database.get_store_credentials(store), ("id", "secret"))

            draft_id = await database.upsert_draft_project(
                "Draft", "https://t.me/+example", "draft-id", "draft-secret", 777,
                "@draft", daily_enabled=True, weekly_enabled=True, monthly_enabled=True,
            )
            draft = (await database.get_all_draft_projects())[0]
            self.assertEqual(draft["id"], draft_id)
            self.assertEqual(draft["daily_enabled"], 1)
            self.assertEqual(draft["monthly_enabled"], 1)
            active_draft_id = await database.activate_draft_project("Draft", 456)
            active_draft = await database.get_store_by_id(active_draft_id)
            self.assertEqual(active_draft["chat_id"], 456)
            self.assertEqual(len(await database.get_all_draft_projects()), 0)

            await database.update_low_balance_state(store_id, is_low=True, alert_sent=True)
            store = await database.get_store_by_id(store_id)
            self.assertEqual(store["low_balance_is_low"], 1)
            self.assertIsNotNone(store["low_balance_last_alert_at"])
            # Both the direct project and the activated draft use the default
            # low-balance monitoring setting.
            self.assertEqual(len(await database.get_balance_monitored_stores()), 2)

            await database.start_low_balance_delivery_cycle(store_id)
            await database.mark_low_balance_destination_sent(store_id, "client")
            store = await database.get_store_by_id(store_id)
            self.assertIsNotNone(store["low_balance_client_sent_at"])
            self.assertIsNone(store["low_balance_team_sent_at"])
            self.assertIsNone(store["low_balance_cycle_completed_at"])

            await database.mark_low_balance_destination_sent(store_id, "team")
            await database.complete_low_balance_delivery_cycle(store_id)
            store = await database.get_store_by_id(store_id)
            self.assertIsNotNone(store["low_balance_team_sent_at"])
            self.assertIsNotNone(store["low_balance_cycle_completed_at"])

            await database.update_low_balance_state(store_id, is_low=False)
            store = await database.get_store_by_id(store_id)
            self.assertEqual(store["low_balance_is_low"], 0)
            self.assertIsNone(store["low_balance_client_sent_at"])
            self.assertIsNone(store["low_balance_team_sent_at"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
