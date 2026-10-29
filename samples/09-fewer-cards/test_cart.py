import unittest

import cart


class TotalTest(unittest.TestCase):
    def test_an_empty_cart_costs_nothing(self) -> None:
        self.assertEqual(cart.total([]), 0)

    def test_each_price_counts_once_per_item(self) -> None:
        self.assertEqual(cart.total([(250, 2), (100, 1)]), 600)


if __name__ == "__main__":
    unittest.main()
