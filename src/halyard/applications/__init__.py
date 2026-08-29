"""Opening the applications on this machine.

Separate from the runtime machinery on purpose. A session Halyard can drive and
an application Halyard can open are different questions about different things,
and the sets only look alike today — opencode is expected to be openable months
before it can be driven, and a runtime can be a command with no application.

Two pieces:

- `catalogue` — what exists, from a shipped data file plus `applications:` in
  halyard.yaml.
- `desktop` — how macOS is asked. Nothing in it names an application.
"""

from halyard.applications.catalogue import Application, known, resolve
from halyard.applications.desktop import Status, available, find, open_, running, status

__all__ = [
    "Application",
    "Status",
    "available",
    "find",
    "known",
    "open_",
    "resolve",
    "running",
    "status",
]
