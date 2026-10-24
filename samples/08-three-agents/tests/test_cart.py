import unittest

import cart
import catalog


class TotalTest(unittest.TestCase):
    def test_the_total_is_each_price_times_its_quantity(self) -> None:
        basket = cart.Cart()
        basket.add("tea", 2)
        basket.add("mug")
        self.assertEqual(basket.total(), 2100)

    def test_a_product_the_catalog_does_not_sell_cannot_be_added(self) -> None:
        with self.assertRaises(catalog.UnknownProductError):
            cart.Cart().add("coffee")


class StockTest(unittest.TestCase):
    def test_as_many_as_are_in_stock_can_be_added(self) -> None:
        basket = cart.Cart()
        basket.add("mug", 2)
        self.assertEqual(basket.lines, {"mug": 2})

    def test_more_than_are_in_stock_are_refused_and_the_cart_is_unchanged(self) -> None:
        basket = cart.Cart()
        basket.add("mug", 2)
        with self.assertRaises(cart.OutOfStockError):
            basket.add("mug")
        self.assertEqual(basket.lines, {"mug": 2})

    def test_a_product_out_of_stock_cannot_be_added_at_all(self) -> None:
        with self.assertRaises(cart.OutOfStockError):
            cart.Cart().add("biscuits")
