Add `with_coupon(items, code)` to `cart.py`: the total once a coupon code is
applied. `SAVE10` takes 10% off, `FIVEOFF` takes 500 cents off but never below
0, and any other code raises `ValueError`. `test_cart.py` already tests it, and
those tests fail until it is done. Change `cart.py` only.
