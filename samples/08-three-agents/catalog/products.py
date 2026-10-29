"""The catalog's own data: what it sells, and at what price, in cents.

Nobody outside the catalog reads this module, the cart included. What the
catalog offers is in `catalog/__init__.py`.
"""

PRICES: dict[str, int] = {
    "tea": 450,
    "mug": 1200,
    "biscuits": 250,
}
