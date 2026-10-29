"""Which forge a host is, and how to reach it.

The host in a repository's remote is the only thing that has to be recognised,
and `gitlab.com` and `github.com` recognise themselves. A self-hosted instance
does not — nothing in `git.example.com` says whether it is GitLab, GitHub
Enterprise or something else — so `forge:` on the project says which, and the
default is only a default.

Each forge has a token of its own, so one machine can label a GitHub project
and a GitLab one: `build` is handed every token there is and takes the one for
the forge it builds.

Adding a provider is a module beside `gitlab.py` and `github.py` and one line
in `BUILDERS`. That is the whole point of `Forge` existing: the rest of Halyard
asks the registry and never learns which provider answered.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from halyard.tasks.github import GitHub
from halyard.tasks.gitlab import GitLab
from halyard.tasks.remotes import Origin
from halyard.tasks.spec import Forge, ForgeError

#: Forge name to the thing that builds one, given where and a token.
BUILDERS: dict[str, Callable[[str, str, str], Forge]] = {
    "github": GitHub,
    "gitlab": GitLab,
}

#: Hosts that name themselves. Anything else has to be told what it is.
KNOWN_HOSTS: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
}

#: The setting each forge's token is read from — so a message can say which one
#: to set.
TOKENS: dict[str, str] = {
    "github": "HALYARD_GITHUB_TOKEN",
    "gitlab": "HALYARD_GITLAB_TOKEN",
}


def kind_of(origin: Origin, declared: str | None = None) -> str | None:
    """Which forge this is: what the project said, or what the host implies."""
    if declared:
        return declared.strip().lower()
    return KNOWN_HOSTS.get(origin.host)


def build(
    origin: Origin, tokens: Mapping[str, str | None], *, declared: str | None = None
) -> Forge:
    """A forge for this repository, with its own token from `tokens` — by
    forge name, as `BUILDERS` names them — or `ForgeError` saying what is
    missing."""
    kind = kind_of(origin, declared)
    known = " or ".join(f"`forge: {name}`" for name in sorted(BUILDERS))
    if kind is None:
        raise ForgeError(
            f"I do not know what kind of forge {origin.host} is. Say so on the project: {known}."
        )
    builder = BUILDERS.get(kind)
    if builder is None:
        raise ForgeError(f"No support for {kind!r} yet. Say {known}.")
    token = tokens.get(kind)
    if not token:
        named = getattr(builder, "name", kind)
        raise ForgeError(f"No {named} token for {origin.host}: set {TOKENS.get(kind, kind)}.")
    return builder(origin.host, origin.path, token)
