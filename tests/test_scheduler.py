from datetime import date
import unittest
from unittest.mock import AsyncMock, patch

import scheduler


class MonthlyScheduleTests(unittest.TestCase):
    def test_monthly_reports_use_the_final_calendar_day(self):
        self.assertTrue(scheduler.is_last_day_of_month(date(2026, 1, 31)))
        self.assertTrue(scheduler.is_last_day_of_month(date(2026, 4, 30)))
        self.assertTrue(scheduler.is_last_day_of_month(date(2028, 2, 29)))
        self.assertFalse(scheduler.is_last_day_of_month(date(2026, 10, 1)))
        self.assertFalse(scheduler.is_last_day_of_month(date(2026, 2, 27)))


class LowBalanceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, **overrides):
        store = {
            "id": 1,
            "store_name": "Test | City",
            "chat_id": 123,
            "client_mention": "@client",
            "user_id": 42,
            "low_balance_is_low": 1,
            "low_balance_last_alert_at": None,
            "low_balance_client_sent_at": "2026-10-01T07:00:00+00:00",
            "low_balance_team_sent_at": None,
            "low_balance_cycle_completed_at": None,
        }
        store.update(overrides)
        return store

    async def test_team_retry_does_not_repeat_client_alert(self):
        store = self._store()
        recipients = []

        async def send(_bot, chat_id, _text):
            recipients.append(chat_id)

        with patch.object(scheduler.database, "get_balance_monitored_stores", AsyncMock(return_value=[store])), \
             patch.object(scheduler.database, "get_store_credentials", AsyncMock(return_value=("id", "secret"))), \
             patch.object(scheduler.avito_api, "get_avito_token", AsyncMock(return_value="token")), \
             patch.object(scheduler.avito_api, "get_balance", AsyncMock(return_value={"wallet": 1000, "advance": 500, "total": 1500})), \
             patch.object(scheduler.database, "mark_low_balance_destination_sent", AsyncMock()) as mark_sent, \
             patch.object(scheduler.database, "complete_low_balance_delivery_cycle", AsyncMock()) as complete, \
             patch.object(scheduler, "_telegram_send_with_retry", send), \
             patch.object(scheduler.config, "LOW_BALANCE_ALERT_CHAT_ID", 999):
            await scheduler.check_low_balances_job(object())

        self.assertEqual(recipients, [999])
        mark_sent.assert_awaited_once_with(1, "team")
        complete.assert_awaited_once_with(1)

    async def test_new_low_balance_cycle_delivers_to_both_destinations(self):
        store = self._store(
            low_balance_is_low=0,
            low_balance_client_sent_at=None,
            low_balance_team_sent_at=None,
        )
        recipients = []

        async def send(_bot, chat_id, _text):
            recipients.append(chat_id)

        with patch.object(scheduler.database, "get_balance_monitored_stores", AsyncMock(return_value=[store])), \
             patch.object(scheduler.database, "get_store_credentials", AsyncMock(return_value=("id", "secret"))), \
             patch.object(scheduler.avito_api, "get_avito_token", AsyncMock(return_value="token")), \
             patch.object(scheduler.avito_api, "get_balance", AsyncMock(return_value={"wallet": 1000, "advance": 500, "total": 1500})), \
             patch.object(scheduler.database, "start_low_balance_delivery_cycle", AsyncMock()) as start_cycle, \
             patch.object(scheduler.database, "mark_low_balance_destination_sent", AsyncMock()) as mark_sent, \
             patch.object(scheduler.database, "complete_low_balance_delivery_cycle", AsyncMock()) as complete, \
             patch.object(scheduler, "_telegram_send_with_retry", send), \
             patch.object(scheduler.config, "LOW_BALANCE_ALERT_CHAT_ID", 999):
            await scheduler.check_low_balances_job(object())

        self.assertEqual(recipients, [123, 999])
        start_cycle.assert_awaited_once_with(1)
        self.assertEqual(mark_sent.await_args_list[0].args, (1, "client"))
        self.assertEqual(mark_sent.await_args_list[1].args, (1, "team"))
        complete.assert_awaited_once_with(1)


if __name__ == "__main__":
    unittest.main()
