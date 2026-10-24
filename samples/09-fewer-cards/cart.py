"""A shopping cart's total, in cents."""


def total(items: list[tuple[int, int]]) -> int:
    """What the items cost together: each price, in cents, times its quantity."""
    return sum(price * quantity for price, quantity in items)
