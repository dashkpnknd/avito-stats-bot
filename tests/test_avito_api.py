import unittest

import avito_api


class BalanceTests(unittest.TestCase):
    def test_cpa_balance_is_used_for_the_second_available_balance(self):
        payload = {"result": {"balance": 265720, "debt": 0, "advance": 100}}
        self.assertEqual(avito_api._cpa_balance_value(payload), 2657.2)


if __name__ == "__main__":
    unittest.main()
