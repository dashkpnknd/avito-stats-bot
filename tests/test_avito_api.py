import unittest

import avito_api


class BalanceTests(unittest.TestCase):
    def test_cpa_balance_is_converted_from_kopeks(self):
        payload = {"result": {"balance": 265720, "debt": 0, "advance": 100}}
        self.assertEqual(avito_api._number((payload["result"] or {}).get("balance")) / 100, 2657.2)


if __name__ == "__main__":
    unittest.main()
