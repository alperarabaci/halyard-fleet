"""What the cart needs from the catalog, written by the cart, the consumer, and
run against the real catalog, the provider. When the catalog changes what it
offers, this says at once whether the cart still gets what it relies on: each
test is one thing the cart uses. See AGENTS.md."""

import unittest

import catalog


class CartNeedsCatalog(unittest.TestCase):
    def test_a_price_in_whole_cents(self) -> None:
        self.assertIsInstance(catalog.price_of("tea"), int)

    def test_a_product_the_catalog_does_not_sell_is_refused(self) -> None:
        with self.assertRaises(catalog.UnknownProductError):
            catalog.price_of("coffee")

    def test_how_many_are_in_stock(self) -> None:
        self.assertIsInstance(catalog.in_stock("tea"), int)
