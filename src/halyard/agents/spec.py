"""What a runtime has to say about itself.

`AgentRunner` describes how to *send* to a runtime, and that turned out to be
the small half. Everything else a runtime needs — where its hooks file lives,
what its shell tool is called, how its settings document is shaped, which CLI
proves it is installed, what a generated seat label should start with — had
nowhere to live, so it went into whichever module in `halyard/` happened to
need it first. Counted before this file existed: about sixty branches on a
runtime name across eight modules that are otherwise about something else.

The cost of that is not ugliness, it is that adding a runtime means finding all
sixty. Three of them were found the hard way — a matcher that gated the CLI and
ignored the desktop app, a trust key built as `pretooluse` where Codex writes
`pre_tool_use`, and a `Stop` handler written in the grouped shape that
Antigravity does not look inside. Each was invisible: the file was present,
`doctor` was happy, and the hook never fired.

So a runtime is a package that describes itself, and `registry.discover()`
finds it. Adding one is adding a package.

**The bridge is deliberately not covered here.** Those four scripts run from a
hook with no virtualenv and no `halyard` on the path — they can import nothing
from this tree, which is why they are stdlib-only in the first place. Their
dialects live in `bridge/dialects.py`, the same table in the one other place it
can be read.
"""

from __future__ import annotations

import importlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from halyard.agents.base import AgentRunner, SessionRef

#: `("PreInvocation", None, "inject.py", 15)` — an event, the matcher it wants
#: (`None` for events that take handlers directly), the bridge script by name,
#: and how long it may block.
#:
#: By name rather than by path: a package saying where this checkout keeps its
#: bridge scripts would be a package that has to be edited when that moves.
HookEntry = tuple[str, str | None, str, int]


def late(module: str, attribute: str) -> Callable:
    """Resolve `module.attribute` when it is called, not when this is read.

    Binding the function object straight into the spec freezes whichever object
    existed at import time — and a module attribute is the seam this project
    already substitutes at, so a test that replaces
    `halyard.agents.claude_code.find_session` would go on calling the original
    and pass for the wrong reason. Late binding keeps the module the one source
    of truth about what its own name means.
    """

    def call(*args, **kwargs):
        return getattr(importlib.import_module(module), attribute)(*args, **kwargs)

    call.__name__ = attribute
    call.__qualname__ = f"{module}.{attribute}"
    return call


@dataclass(frozen=True)
class Hooks:
    """Where a runtime reads its hooks, and what shape it expects them in.

    Three runtimes, three answers, and the differences are not cosmetic — each
    one has already cost a gate that reported itself installed and never fired.
    """

    #: Relative to the repository root.
    settings: str
    #: What matches this runtime's shell tool. A regular expression for Codex
    #: and Antigravity; a plain name for Claude Code.
    matcher: str
    #: `wrapped` is `{"hooks": {Event: [group, ...]}}` — Claude Code and Codex.
    #: `named` is `{"<hook name>": {Event: ...}}`, which is Antigravity's: the
    #: top level is a namespace, and every name contributing to an event is
    #: merged and run in turn.
    #: Other files this runtime also reads hooks from, checked but never
    #: written. Claude Code is the case: a committed `settings.json` beside the
    #: gitignored `settings.local.json`, and a gate may be in either.
    also: tuple[str, ...] = ()
    dialect: str = "wrapped"
    #: Events beyond the two every runtime gets (the gate and the relay).
    extra: tuple[HookEntry, ...] = ()
    #: Events that wrap their handlers in a `matcher`/`hooks` group. Only the
    #: `named` dialect distinguishes: it takes the tool events grouped and
    #: every other event as a flat list of handler objects, and a handler
    #: written in the wrong one is not found.
    grouped: tuple[str, ...] = ()
    #: True when a hook can be switched off in place, so `doctor` should look.
    #: Antigravity's `"enabled": false` leaves a file that reads as wired and
    #: runs none of it.
    disableable: bool = False


@dataclass(frozen=True)
class Verification:
    """How to drive one non-interactive turn, for `halyard verify`.

    Separate from everything above because it describes a *harness*, not the
    runtime as used. It exists on the spec anyway for the same reason the rest
    does: a fourth runtime that needs an entry in a table inside `verify.py` is
    a fourth runtime you cannot add by dropping in a package.

    `None` is a real answer. Antigravity has no way to run one turn and exit —
    the CLI it does have keeps its conversations in a store the application
    cannot see — so it cannot be verified this way and says so, instead of
    being quietly skipped by a name check somewhere else.
    """

    #: Argument list for one turn. `"{prompt}"` is replaced by the harness.
    command: tuple[str, ...]
    #: How long the hook may block. Short: the harness is measuring whether it
    #: fires, not what it decides.
    hook_timeout: int
    #: The harness writes its own settings file and can be narrower than the
    #: real gate — it drives one front end, not both.
    matcher: str
    #: Extra keys the throwaway settings file needs, given the command the
    #: harness is about to run. Claude Code needs its own permission flow
    #: allow-listed or it stops the command before the gate ever sees it, and
    #: every case reads as a pass.
    settings_extra: Callable[[str], dict] | None = None
    #: Whether a run that never mentions the hook at all is worth reporting.
    expects_hook_mention: bool = False


@dataclass(frozen=True)
class RuntimeSpec:
    """One runtime, described by its own package.

    Everything here was previously a branch somewhere in `halyard/`. Nothing
    here is a preference: each field exists because some module could not do
    its job without knowing it, and reached for a runtime name to find out.
    """

    #: What a seat's `runtime:` says, and the key everything is looked up by.
    name: str
    #: For a person reading a prompt or a report.
    human: str
    #: The CLI whose presence means this runtime is worth wiring.
    binary: str
    hooks: Hooks
    #: Built once per control plane and shared by every seat that uses it.
    #: Called with the `Settings` object, which most runtimes ignore — Claude
    #: Code is the only one with settings of its own, and knowing that was a
    #: branch in `create_app` until each package took its own factory.
    runner: Callable[..., AgentRunner]
    #: A session by name or id, asked of the runtime that owns the name. Two
    #: runtimes hold the same name at once often enough that this is never a
    #: lookup anybody else can do — see `seats.for_session`.
    find_session: Callable[[str], SessionRef | None]
    #: Named sessions as (name, id, last modified), newest first. Offered as
    #: suggestions by `halyard init`.
    list_sessions: Callable[[], list[tuple[str, str, float]]]
    #: What a generated seat label starts with, so three runtimes' defaults are
    #: not all `drv1`. Matches what people were already writing by hand:
    #: `nav`/`drv`, `xnav`/`xdrv`, `gnav`/`gdrv`.
    prefix: str = ""
    #: Whether this runtime is on the machine, when a PATH lookup is not the
    #: answer. Antigravity's application bundles its binaries inside the `.app`
    #: and puts nothing on PATH, so `which` reports it missing on the one
    #: machine where it is certainly installed.
    present: Callable[[], bool] | None = None
    #: Where to look for a name `doctor` could not resolve. Every runtime keeps
    #: its sessions somewhere else, and "no session named X" is only useful
    #: next to where X was supposed to come from.
    sessions_hint: str = ""
    #: Can this runtime be reached at all, right now? Returns `(level, text)`
    #: pairs; a `fail` stops the seat being checked further. Called before the
    #: session is looked up, because a missing CLI makes every later answer
    #: meaningless. Given whatever context `doctor` has — Claude Code is the
    #: only runtime that takes a configured binary path, and it reads it from
    #: the keyword arguments rather than making everyone else accept one.
    check_available: Callable[..., list[tuple[str, str]]] | None = field(default=None)
    #: Anything worth saying about a session that *did* resolve. This is where
    #: findable-and-unreachable lives: an Antigravity conversation owned by the
    #: `agy` CLI resolves perfectly and can never be sent to, and without this
    #: the seat reads as configured and working.
    check_session: Callable[..., list[tuple[str, str]]] | None = field(default=None)
    #: Whether the hooks written into a project will actually run. Codex is the
    #: one runtime that can be wired correctly and still have no gate: it skips
    #: a hook nobody trusted, in silence, and refuses a whole hooks file over
    #: one field it does not recognise. Called with the hooks file and the
    #: project, by both `wire` and `doctor`.
    check_wired: Callable[..., list[tuple[str, str]]] | None = field(default=None)
    #: How `halyard verify` drives this runtime, when it can at all.
    verify: Verification | None = None

    def on_this_machine(self) -> bool:
        return self.present() if self.present else bool(shutil.which(self.binary))

    def settings_path(self, project_root: Path) -> Path:
        return project_root / self.hooks.settings
