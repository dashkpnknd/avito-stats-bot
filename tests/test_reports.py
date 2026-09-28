from datetime import date
import unittest

from reports import (
    daily_report_periods,
    format_low_balance_client_alert,
    format_low_balance_team_alert,
    format_daily_report,
    report_period,
)


class ReportPeriodTests(unittest.TestCase):
    def test_daily_report_is_for_yesterday(self):
        self.assertEqual(
            report_period("daily", date(2026, 9, 28)),
            (date(2026, 9, 27), date(2026, 9, 27), date(2026, 9, 26), date(2026, 9, 26)),
        )

    def test_weekly_report_is_for_previous_complete_week(self):
        self.assertEqual(
            report_period("weekly", date(2026, 9, 28)),
            (date(2026, 9, 21), date(2026, 9, 27), date(2026, 9, 14), date(2026, 9, 20)),
        )


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.balance = {"wallet": 1200, "advance": 1700, "total": 2900}

    def test_report_has_required_daily_and_weekly_blocks(self):
        text = format_daily_report(
            "Test <shop>",
            date(2026, 9, 27),
            {"views": 5, "contacts": 1, "spend": 0},
            date(2026, 9, 21),
            date(2026, 9, 27),
            {"views": 50, "contacts": 10, "spend": 1000, "calls": 3},
            {"views": 40, "contacts": 8, "spend": 800, "calls": 1},
            self.balance,
        )
        self.assertIn("<b>Вчера</b> (27.09)", text)
        self.assertIn("<b>Неделя</b> (21.09 – 27.09)", text)
        self.assertIn("💰 Расходы: 1 000   ▲ +25% ₽", text)
        self.assertIn("💳 Кошелёк: <b>1 200 ₽</b> | Аванс: <b>1 700 ₽</b>", text)
        self.assertIn("TEST &lt;SHOP&gt;", text)

    def test_daily_report_uses_rolling_completed_week(self):
        self.assertEqual(
            daily_report_periods(date(2026, 9, 28)),
            (date(2026, 9, 27), date(2026, 9, 21), date(2026, 9, 27), date(2026, 9, 14), date(2026, 9, 20)),
        )

    def test_low_balance_alert_has_required_action(self):
        text = format_low_balance_client_alert("Test", "@client", self.balance)
        self.assertIn("Общий остаток: <b>2900.00 ₽</b>", text)
        self.assertIn("объявления не пропали из поиска", text)
        self.assertIn("Сегодня получится пополнить?", text)

    def test_team_alert_is_aggregated(self):
        text = format_low_balance_team_alert([("A", "@a", self.balance), ("B", "", self.balance)])
        self.assertEqual(text.count("<b>Итого: 2900.00 ₽</b>"), 2)
        self.assertIn("<b>A</b> — @a", text)


if __name__ == "__main__":
    unittest.main()
