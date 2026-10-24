"""The cart: what somebody is about to buy.

Prices come from the catalog, through its public face, never from inside it.
See AGENTS.md.
"""

import catalog

__all__ = ["Cart"]


class Cart:
    """Products, and how many of each, by sku."""

    def __init__(self) -> None:
        self.lines: dict[str, int] = {}

    def add(self, sku: str, quantity: int = 1) -> None:
        """Put `quantity` more of `sku` in the cart."""
        catalog.price_of(sku)  # a product the catalog does not sell is refused here
        self.lines[sku] = self.lines.get(sku, 0) + quantity

    def total(self) -> int:
        """What the cart comes to, in cents."""
        return sum(catalog.price_of(sku) * quantity for sku, quantity in self.lines.items())
