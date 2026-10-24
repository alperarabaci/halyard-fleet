"""The catalog: what is for sale, and at what price.

This module is the catalog's public face: what `__all__` lists is all another
context may use of it. See AGENTS.md.
"""

from catalog.products import PRICES

__all__ = ["UnknownProductError", "price_of"]


class UnknownProductError(KeyError):
    """A product the catalog does not sell."""


def price_of(sku: str) -> int:
    """What one of `sku` costs, in cents."""
    try:
        return PRICES[sku]
    except KeyError:
        raise UnknownProductError(sku) from None
