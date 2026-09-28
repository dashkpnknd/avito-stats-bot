from datetime import date
import unittest

from reports import (
    format_low_balance_client_alert,
    format_low_balance_team_alert,
    format_report,
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

    def test_report_has_current_balance(self):
        text = format_report(
            "Test <shop>",
            "daily",
            date(2026, 9, 27),
            date(2026, 9, 27),
            {"views": 5, "contacts": 1, "spend": 0},
            {"views": 4, "contacts": 1, "spend": 0},
            self.balance,
        )
        self.assertIn("Бюджет на текущий момент", text)
        self.assertIn("Кошелёк: <b>1200.00 ₽</b>", text)
        self.assertIn("Test &lt;shop&gt;", text)

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
