from datetime import datetime, timezone
import unittest

import avito_api


class BalanceTests(unittest.TestCase):
    def test_cpa_balance_is_used_for_the_second_available_balance(self):
        payload = {"result": {"balance": 265720, "debt": 0, "advance": 100}}
        self.assertEqual(avito_api._cpa_balance_value(payload), 2657.2)

    def test_429_is_distinguished_from_other_api_errors(self):
        self.assertIsInstance(
            avito_api._response_error("stats", 429, ""), avito_api.AvitoRateLimitError
        )
        self.assertIsInstance(
            avito_api._response_error("stats", 400, ""), avito_api.AvitoAPIError
        )

    def test_retry_error_keeps_a_useful_timeout_description(self):
        async def call():
            raise TimeoutError()

        with self.assertRaisesRegex(avito_api.AvitoAPIError, "TimeoutError"):
            import asyncio
            asyncio.run(avito_api._request_with_retry(call, attempts=1))


class TariffDateTests(unittest.TestCase):
    def test_add_tariff_period_is_fixed_thirty_days(self):
        value = datetime(2026, 10, 3, 14, 34, tzinfo=timezone.utc)
        self.assertEqual(
            avito_api.add_tariff_period(value),
            datetime(2026, 11, 2, 14, 34, tzinfo=timezone.utc),
        )

    def test_add_tariff_period_does_not_use_calendar_month(self):
        value = datetime(2027, 1, 31, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            avito_api.add_tariff_period(value),
            datetime(2027, 3, 2, 10, 0, tzinfo=timezone.utc),
        )

    def test_tariff_date_requires_two_consecutive_months(self):
        self.assertTrue(
            avito_api.has_confirmed_monthly_tariff_cycle([
                datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc),
            ])
        )
        self.assertFalse(
            avito_api.has_confirmed_monthly_tariff_cycle([
                datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
            ])
        )


if __name__ == "__main__":
    unittest.main()
