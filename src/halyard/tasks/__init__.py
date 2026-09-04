"""Labelling the task a branch is for.

Its own package, sharing nothing with the rest. Until this, Halyard needed only
git — which is why it worked the same against GitLab, GitHub or a directory on
a server. Reaching an issue tracker is the first thing that cannot be provider-
neutral, so the provider is behind a contract rather than spread through the
code that uses it.

The pieces: `spec` says what a forge must answer, `remotes` reads where a
repository points, `branches` says which task a branch is for, `registry` turns
a host into a forge, `labelling` decides what is worth offering, and `gitlab`
is the one implementation there is today.
"""

from halyard.tasks.branches import current as current_branch
from halyard.tasks.branches import number_of
from halyard.tasks.labelling import Choice, put_on, to_offer, worth_offering
from halyard.tasks.registry import BUILDERS, KNOWN_HOSTS, build, kind_of
from halyard.tasks.remotes import Origin, origin_of
from halyard.tasks.remotes import read as read_remote
from halyard.tasks.spec import Forge, ForgeError, Task

__all__ = [
    "BUILDERS",
    "KNOWN_HOSTS",
    "Choice",
    "Forge",
    "ForgeError",
    "Origin",
    "Task",
    "build",
    "current_branch",
    "kind_of",
    "number_of",
    "origin_of",
    "put_on",
    "read_remote",
    "to_offer",
    "worth_offering",
]
