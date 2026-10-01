from datetime import date
import unittest

from scheduler import is_last_day_of_month


class MonthlyScheduleTests(unittest.TestCase):
    def test_monthly_reports_use_the_final_calendar_day(self):
        self.assertTrue(is_last_day_of_month(date(2026, 1, 31)))
        self.assertTrue(is_last_day_of_month(date(2026, 4, 30)))
        self.assertTrue(is_last_day_of_month(date(2028, 2, 29)))
        self.assertFalse(is_last_day_of_month(date(2026, 10, 1)))
        self.assertFalse(is_last_day_of_month(date(2026, 2, 27)))


if __name__ == "__main__":
    unittest.main()
