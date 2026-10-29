Products can run out. The catalog says how many of each it has left —
`catalog.in_stock(sku)`: tea 10, mug 2, biscuits 0 — and the cart refuses to
hold more of a product than that, with `cart.OutOfStockError`. The contract and
both contexts' tests already say what is wanted, and fail until it is done.
Change `catalog/` and `cart/` only.
