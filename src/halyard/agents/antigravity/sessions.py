"""Finding an Antigravity conversation by the name a person gave it.

Three files, none of which alone is enough — measured:

- the **name** is in `~/.gemini/antigravity/annotations/<id>.pbtxt`, one line of
  protobuf text format: `title:"alpha-engine-driver" last_user_view_time:{...}`.
  A conversation nobody renamed simply has no `title:` field.
- the **directory** is not in the transcript at all. Claude Code and Codex both
  record theirs; Antigravity's transcript carries `content`, `created_at`,
  `source`, `status`, `step_index`, `thinking`, `tool_calls` and `type`, and
  nothing about where the work is happening.
- it *is* in `conversations/<id>.db`, inside a protobuf blob. That is not read
  here. The Codex postmortem already refused a milder version of the same idea —
  a database whose filename carries its schema version — and an undocumented
  blob is worse.

So the directory comes from `agentapi get-conversation-metadata`, which returns
it plainly, and needs the application running. Everything that can be answered
from files is answered from files, so a seat can still be *listed* with the app
shut; only resolving its directory needs the app.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime
from pathlib import Path

from halyard.agents.base import SessionRef

logger = logging.getLogger(__name__)

#: `title:"..."` — protobuf text format, and the whole of what is needed from it.
_TITLE = re.compile(r'title:\s*"((?:[^"\\]|\\.)*)"')


#: Two stores, and they share nothing. The desktop application keeps its
#: conversations in the first; `agy`, the CLI, keeps its own in the second, with
#: a parallel `brain/` and `conversations/` of its own. A conversation started
#: in one is invisible in the other — measured, and it is the reason a CLI
#: session does not appear in the app the way a Codex one does.
#:
#: Which store a conversation lives in decides how a message reaches it, so the
#: lookup returns that alongside the session rather than leaving the runner to
#: guess.
APP_HOME = Path.home() / ".gemini" / "antigravity"
CLI_HOME = Path.home() / ".gemini" / "antigravity-cli"


def antigravity_home(root: Path | None = None) -> Path:
    return root or APP_HOME


def homes(root: Path | None = None) -> list[Path]:
    """Every store to look in, the application's first."""
    return [root] if root else [APP_HOME, CLI_HOME]


def home_of(conversation_id: str, root: Path | None = None) -> Path | None:
    """Which store holds this conversation, or nothing if neither does."""
    for home in homes(root):
        if (home / "brain" / conversation_id).is_dir():
            return home
    return None


def is_cli(conversation_id: str, root: Path | None = None) -> bool:
    """Whether this conversation belongs to `agy` rather than the application.

    The two are reached differently: the CLI has `agy -p --conversation`, which
    works whether or not anything is open, and the application has an IPC
    surface that needs it running.
    """
    return home_of(conversation_id, root) == CLI_HOME


def _titles(root: Path | None = None) -> dict[str, str]:
    """Every conversation that has been given a name, as name → id.

    Across both stores. A name used in each would collide, and the later one
    wins — the same rule as a name reused within one store, and the same
    consequence, which is why `doctor` prints which store a seat resolved in.
    """
    found: dict[str, str] = {}
    annotations: list[Path] = []
    for home in homes(root):
        try:
            annotations += list((home / "annotations").glob("*.pbtxt"))
        except OSError:
            continue
    with contextlib.suppress(OSError):
        annotations.sort(key=lambda p: p.stat().st_mtime)
    for path in annotations:
        try:
            match = _TITLE.search(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if match:
            # Newest last, so a name reused on a later conversation wins.
            found[match.group(1)] = path.stem
    return found


def transcript_for(conversation_id: str, root: Path | None = None) -> Path:
    return (
        (home_of(conversation_id, root) or antigravity_home(root))
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )


def describe(conversation_id: str, name: str, *, root: Path | None = None) -> SessionRef:
    """What is known without asking the application.

    `cwd` is left unset here on purpose. It is available — from
    `get-conversation-metadata` — but only while Antigravity is running, and a
    listing should not fail because an application is closed. The runner fills
    it in when it needs it.
    """
    return SessionRef(session_id=conversation_id, name=name, cwd=None)


def find_session(name: str, *, root: Path | None = None) -> SessionRef | None:
    """Resolve a title — or a raw conversation id — to a session."""
    wanted = name.strip()
    if not wanted:
        return None

    titles = _titles(root)
    if wanted in titles:
        return describe(titles[wanted], wanted, root=root)

    folded = {key.casefold(): value for key, value in titles.items()}
    if wanted.casefold() in folded:
        return describe(folded[wanted.casefold()], wanted, root=root)

    # An id is accepted too: a conversation nobody renamed has no title, and
    # telling somebody to go and name it before it can be addressed is not an
    # answer when the id is right there in the app.
    if home_of(wanted, root) is not None:
        return describe(wanted, wanted, root=root)
    return None


def list_named_sessions(*, root: Path | None = None) -> list[SessionRef]:
    """Every named conversation, newest first.

    `cwd` is unset: Antigravity records the workspace in the hook payload and
    nowhere a listing can reach, which is honest to leave blank rather than
    guess at.
    """
    refs = []
    for name, conversation_id in _titles(root).items():
        home = home_of(conversation_id, root) or antigravity_home(root)
        annotation = home / "annotations" / f"{conversation_id}.pbtxt"
        try:
            modified = datetime.fromtimestamp(annotation.stat().st_mtime)
        except OSError:
            modified = None
        refs.append(
            SessionRef(session_id=conversation_id, name=name, cwd=None, last_active=modified)
        )
    return sorted(refs, key=lambda ref: -(ref.last_active.timestamp() if ref.last_active else 0.0))
