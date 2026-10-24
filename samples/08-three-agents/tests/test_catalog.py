import unittest

import catalog


class PriceTest(unittest.TestCase):
    def test_a_product_has_its_price_in_cents(self) -> None:
        self.assertEqual(catalog.price_of("tea"), 450)

    def test_a_product_the_catalog_does_not_sell_is_refused(self) -> None:
        with self.assertRaises(catalog.UnknownProductError):
            catalog.price_of("coffee")


class StockTest(unittest.TestCase):
    def test_each_product_says_how_many_are_left(self) -> None:
        self.assertEqual(
            {sku: catalog.in_stock(sku) for sku in ("tea", "mug", "biscuits")},
            {"tea": 10, "mug": 2, "biscuits": 0},
        )

    def test_a_product_the_catalog_does_not_sell_has_no_stock_to_tell(self) -> None:
        with self.assertRaises(catalog.UnknownProductError):
            catalog.in_stock("coffee")
