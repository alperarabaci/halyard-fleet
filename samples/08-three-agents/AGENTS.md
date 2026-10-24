# Agents

Five invariants, and no more. Every line here is read on every turn, so a long
list is noise that buries the lines that matter. The ones that matter most are
checked as well as written: by `scripts/guard.py`, by `contracts/`, and at close.

1. Two contexts: `catalog` owns products, prices and stock; `cart` owns what is
   being bought.
2. `cart` uses `catalog` only through its public face — `import catalog`, and
   what `catalog/__init__.py` lists in `__all__` — never a module inside it.
3. `catalog` knows nothing of `cart`.
4. Money is whole cents.
5. What `cart` needs from `catalog` is in `contracts/`: change it together with
   what `catalog` offers.

`make test-fast` runs the boundary guard, the contract and the tests, in that
order.
