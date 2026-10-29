The text below is the driver's report on the task in `TASK.md`. Read; change
nothing.

The project has two contexts, `catalog` and `cart`; `AGENTS.md` says what each
owns and how one may use the other. `scripts/guard.py` already checks the
imports. Look for what it cannot see, in the change itself — `git diff`, and the
files it touched:

- knowledge in the wrong context: the cart keeping its own count of stock, or
  working out a price, rather than asking the catalog;
- the catalog shaped around the cart: something there only because the cart
  needed it, named for the cart, or knowing what a cart is;
- the contract out of step: the catalog offering something `contracts/` does
  not say the cart needs, or the cart using something the contract does not list.

Name each, with where it is and which context it belongs in. If the change keeps
both contexts whole, say so in a line, and only after reading all of it. What you
could not read is not measured. Keep it under fifteen lines.
