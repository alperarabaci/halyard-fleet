"""A shopping cart's total, in cents."""


def total(items: list[tuple[int, int]], discount: int = 0) -> int:
    """What the items cost together — each price, in cents, times its quantity —
    with `discount` percent off."""
    return sum(price * quantity for price, quantity in items) * (100 - discount) // 100


def shipping(items: list[tuple[int, int]]) -> int:
    """What delivery costs, in cents: free once the items come to 5000 or more."""
    return 0 if total(items) >= 5000 else 499
