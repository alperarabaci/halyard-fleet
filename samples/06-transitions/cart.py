"""A shopping cart's total, in cents."""


def total(items: list[tuple[int, int]], discount: int = 0) -> int:
    """What the items cost together — each price, in cents, times its quantity —
    with `discount` percent off."""
    return sum(price * quantity for price, quantity in items) * (100 - discount) // 100
