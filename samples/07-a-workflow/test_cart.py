import unittest

import cart


class TotalTest(unittest.TestCase):
    def test_each_price_counts_once_per_item(self) -> None:
        self.assertEqual(cart.total([(250, 2), (100, 1)]), 600)

    def test_a_discount_takes_a_percentage_off(self) -> None:
        self.assertEqual(cart.total([(250, 2), (100, 1)], discount=10), 540)


class ShippingTest(unittest.TestCase):
    def test_shipping_costs_499_below_5000(self) -> None:
        self.assertEqual(cart.shipping([(1000, 2)]), 499)

    def test_shipping_is_free_from_5000(self) -> None:
        self.assertEqual(cart.shipping([(2500, 2)]), 0)


class CouponTest(unittest.TestCase):
    def test_save10_takes_10_percent_off(self) -> None:
        self.assertEqual(cart.with_coupon([(1000, 2)], "SAVE10"), 1800)

    def test_fiveoff_takes_500_off(self) -> None:
        self.assertEqual(cart.with_coupon([(1000, 2)], "FIVEOFF"), 1500)

    def test_fiveoff_never_goes_below_zero(self) -> None:
        self.assertEqual(cart.with_coupon([(300, 1)], "FIVEOFF"), 0)

    def test_an_unknown_code_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            cart.with_coupon([(1000, 1)], "FREE")


if __name__ == "__main__":
    unittest.main()
