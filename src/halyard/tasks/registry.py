"""Which forge a host is, and how to reach it.

The host in a repository's remote is the only thing that has to be recognised,
and `gitlab.com` recognises itself. A self-hosted instance does not — nothing
in `git.example.com` says whether it is GitLab, Gitea or something else — so
`forge:` on the project says which, and the default is only a default.

Adding GitHub is a module beside `gitlab.py` and one line in `BUILDERS`. That
is the whole point of `Forge` existing: the rest of Halyard asks the registry
and never learns which provider answered.
"""

from __future__ import annotations

from collections.abc import Callable

from halyard.tasks.gitlab import GitLab
from halyard.tasks.remotes import Origin
from halyard.tasks.spec import Forge, ForgeError

#: Forge name to the thing that builds one, given where and a token.
BUILDERS: dict[str, Callable[[str, str, str], Forge]] = {
    "gitlab": GitLab,
}

#: Hosts that name themselves. Anything else has to be told what it is.
KNOWN_HOSTS: dict[str, str] = {
    "gitlab.com": "gitlab",
}


def kind_of(origin: Origin, declared: str | None = None) -> str | None:
    """Which forge this is: what the project said, or what the host implies."""
    if declared:
        return declared.strip().lower()
    return KNOWN_HOSTS.get(origin.host)


def build(origin: Origin, token: str, *, declared: str | None = None) -> Forge:
    """A forge for this repository, or `ForgeError` saying what is missing."""
    kind = kind_of(origin, declared)
    if kind is None:
        raise ForgeError(
            f"I do not know what kind of forge {origin.host} is. "
            f"Say so with `forge:` on the project — one of: {', '.join(sorted(BUILDERS))}."
        )
    builder = BUILDERS.get(kind)
    if builder is None:
        raise ForgeError(f"No support for {kind!r} yet. Known: {', '.join(sorted(BUILDERS))}.")
    if not token:
        raise ForgeError(f"No token configured for {origin.host}.")
    return builder(origin.host, origin.path, token)
