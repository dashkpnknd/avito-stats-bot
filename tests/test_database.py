import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet


_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DB_NAME"] = str(Path(_TEMP_DIR.name) / "test.sqlite3")
os.environ["CREDENTIALS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["ALLOW_FIRST_ADMIN"] = "1"

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

            await database.update_low_balance_state(store_id, is_low=True, alert_sent=True)
            store = await database.get_store_by_id(store_id)
            self.assertEqual(store["low_balance_is_low"], 1)
            self.assertIsNotNone(store["low_balance_last_alert_at"])
            self.assertEqual(len(await database.get_balance_monitored_stores()), 1)
            self.assertTrue(await database.claim_first_admin(777))
            self.assertFalse(await database.claim_first_admin(888))
            self.assertTrue(await database.is_admin(777))
            self.assertTrue(await database.add_admin(888))
            self.assertTrue(await database.remove_admin(888))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
