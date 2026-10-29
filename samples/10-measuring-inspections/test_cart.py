import unittest

import cart


class TotalTest(unittest.TestCase):
    def test_each_price_counts_once_per_item(self) -> None:
        self.assertEqual(cart.total([(250, 2), (100, 1)]), 600)

    def test_shipping_is_free_from_5000(self) -> None:
        self.assertEqual(cart.shipping([(2500, 2)]), 0)


class BulkDiscountTest(unittest.TestCase):
    def test_ten_or_more_of_an_item_take_5_percent_off_that_line(self) -> None:
        self.assertEqual(cart.bulk_discount([(100, 10)]), 950)

    def test_fewer_than_ten_pay_the_full_price(self) -> None:
        self.assertEqual(cart.bulk_discount([(100, 9)]), 900)

    def test_a_mix_of_lines(self) -> None:
        self.assertGreater(cart.bulk_discount([(100, 10), (250, 2)]), 0)


if __name__ == "__main__":
    unittest.main()
