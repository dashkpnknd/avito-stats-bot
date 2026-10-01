import unittest

import avito_api


class BalanceTests(unittest.TestCase):
    def test_cpa_advance_is_read_from_its_own_field(self):
        payload = {"result": {"balance": 265720, "debt": 0, "advance": 100}}
        self.assertEqual(avito_api._cpa_advance_value(payload), 1.0)


if __name__ == "__main__":
    unittest.main()
