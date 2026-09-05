"""Halyard Fleet — a control plane for orchestrating coding agents remotely."""

#: What the running code reports as its version.
#:
#: This line said `0.1.0` through six releases. Not because anybody got it
#: wrong, but because nothing imported it: a copy nobody reads is a copy nobody
#: can notice rotting. `halyard.api.app` now reads it, and
#: `tests/test_version.py` holds it to what `pyproject.toml` declares, so the
#: two disagreeing is a failing test rather than a slow lie.
#:
#: There is an obvious way to have one copy instead of two —
#: `importlib.metadata.version("halyard-fleet")` — and it was tried and
#: rejected. An editable install carries whatever version it was installed with,
#: while the code it runs comes from this tree, so between a bump and a
#: reinstall the metadata describes something other than what is executing.
#: Measured on a throwaway package: the file said `0.9.9` and the metadata went
#: on saying `0.1.0`. Reading it here would make the one number the code reports
#: about itself the one number that can be wrong about the code.
#:
#: Declaring the version dynamic in `pyproject.toml` and sourcing it from this
#: line was tried too, and is worse in a quieter way: with nothing static to
#: compare, uv stops noticing the version changed at all. Measured — after
#: editing this line, neither `uv run` nor `uv sync` rebuilt, and the installed
#: metadata kept answering `0.7.1` until an explicit `--reinstall-package`. One
#: copy that is permanently stale is not an improvement on two copies a test
#: keeps together.
__version__ = "0.7.1"
