import unittest

from cart import total


class TotalTest(unittest.TestCase):
    def test_an_empty_cart_costs_nothing(self) -> None:
        self.assertEqual(total([]), 0)

    def test_each_price_counts_once_per_item(self) -> None:
        self.assertEqual(total([(250, 2), (100, 1)]), 600)

    def test_a_discount_takes_a_percentage_off(self) -> None:
        self.assertEqual(total([(250, 2), (100, 1)], discount=10), 540)


if __name__ == "__main__":
    unittest.main()
