"""Where the bridges find the control plane, without being told every time.

A hook runs in whatever environment Claude Code was launched with. Requiring
`HALYARD_URL` to be exported in that shell means remembering it in every
terminal, on every machine, forever — and forgetting it does not produce a
helpful error. It produces a denied command, because the approval bridge fails
closed on a control plane it cannot reach.

So the bridges look it up instead. The address is a fact about the machine, and
it is already written down in the `halyard.yaml` the control plane reads.
Nothing needs to be configured twice.

Standard library only, and it never raises: a bridge that crashes reading its
own configuration is worse than one that falls back to the default.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8787"

#: Where the bridge writes down that it ran. Beside the control plane's own log
#: rather than inside it, because these are the lines the control plane never
#: sees: a hook that could not reach it, a hook that denied before asking, a
#: hook nobody is sure fired at all.
#:
#: That last one is why this exists. Whether a runtime actually invoked the
#: hook was, until now, unanswerable — the control plane records what arrives
#: and can say nothing about what never left.
BRIDGE_LOG = Path(__file__).resolve().parent.parent / "bridge.log"

#: Searched in order, first hit wins. `halyard.yaml` is the file the control
#: plane is configured from; the home location exists for installs where the
#: bridges are referenced from somewhere else.
_CONFIG_FILES = (
    Path(__file__).resolve().parent.parent / "halyard.yaml",
    Path(__file__).resolve().parent.parent / "halyard.yml",
    Path.home() / ".halyard" / "config",
)


def _read_key(path: Path, key: str) -> str | None:
    """One scalar out of a `settings:` block, without a YAML parser.

    These scripts import nothing outside the standard library, on purpose —
    they run from a hook, in somebody else's process tree, with no virtualenv
    on the path. There is no `yaml` here and there will not be one.

    What is read is deliberately tiny: `KEY: value` lines, indented, under a
    top-level `settings:`. That covers what a bridge needs — an address —
    and nothing else. Anchors, nested maps, multi-line scalars and flow style
    are not understood, and a file using them for these two keys simply looks
    unset, which falls back to the default rather than to a wrong address.

    `KEY=value` is still accepted so an old `.halyard/config` keeps working.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    inside = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1].isspace()
        if not indented:
            # A new top-level key ends the block this cares about.
            inside = raw.split(":", 1)[0].strip() == "settings"
            if "=" in raw:
                name, _, value = raw.partition("=")
                if name.strip() == key:
                    return value.strip().strip("\"'") or None
            continue
        if not inside:
            continue
        name, sep, value = raw.partition(":")
        if sep and name.strip() == key:
            return value.strip().strip("\"'") or None
    return None


def lookup(key: str, default: str) -> str:
    """Return `key` from the environment, then the config files, then `default`."""
    from_env = os.environ.get(key)
    if from_env:
        return from_env
    for path in _CONFIG_FILES:
        value = _read_key(path, key)
        if value:
            return value
    return default


def _url_from_bind(bind: str) -> str:
    """Turn `host:port` into the address a client on this machine would use.

    A server bound to every interface is still reached over loopback, and a
    bridge asked to connect to 0.0.0.0 is a bridge about to deny everything.
    """
    host, _, port = bind.rpartition(":")
    if not port.isdigit():
        return DEFAULT_URL
    if host in ("", "0.0.0.0", "::", "[::]", "*"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def control_plane_url() -> str:
    """Where the control plane is reachable from this machine.

    Derived from `HALYARD_BIND`, which is the one place the address is written
    down. Three keys that had to be kept in agreement by hand — the bind, the
    published Docker port, and a URL — is three chances to get it wrong, and
    getting it wrong denies every command with a message about a port.

    `HALYARD_URL` still overrides, for the case the derivation cannot cover: a
    control plane on another machine, reached over Tailscale or WireGuard.
    """
    explicit = lookup("HALYARD_URL", "")
    if explicit:
        return explicit
    bind = lookup("HALYARD_BIND", "")
    return _url_from_bind(bind) if bind else DEFAULT_URL


def timeout(key: str, default: float) -> float:
    try:
        return float(lookup(key, str(default)))
    except ValueError:
        return default


#: How much of a transcript's tail to read looking for its title. Measured on a
#: 5 MB transcript: titles are rewritten repeatedly through the last few percent
#: of the file, and reading this much costs about a tenth of a millisecond.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def antigravity_title(conversation_id: str | None, home: Path | None = None) -> str | None:
    """An Antigravity conversation's name, from its annotation file.

    One line of protobuf text format — `title:"alpha-engine-driver" ...` — and
    nothing else in the conversation carries it. Read here rather than in core
    so the bridge can answer the only question routing asks, which seat this
    is, with the standard library alone.
    """
    if not conversation_id:
        return None
    root = home or Path.home() / ".gemini" / "antigravity"
    path = root / "annotations" / f"{conversation_id}.pbtxt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'title:\s*"((?:[^"\\]|\\.)*)"', text)
    return match.group(1) if match else None


def antigravity_reply(transcript_path: str | None) -> str | None:
    """What the agent last said, read out of its own transcript.

    The other two runtimes hand the turn's final message to the `Stop` hook.
    Antigravity's `Stop` payload carries no text at all — `executionNum`,
    `terminationReason`, `error`, `fullyIdle`, and the common fields — so the
    reply has to be read from `transcriptPath`, where each line is a record and
    the assistant's own turns are typed `PLANNER_RESPONSE`.

    Only records that finished are considered. A `PLANNER_RESPONSE` is written
    before it is complete, and relaying a half-formed one would send a sentence
    that stops mid-word and never correct it.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "PLANNER_RESPONSE":
            continue
        if record.get("status") not in (None, "DONE"):
            continue
        content = record.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def runtime_of(transcript_path: str | None) -> str:
    """Which agent produced this payload, from where it keeps its transcript.

    Codex files rollouts under `~/.codex/sessions/`, Claude Code under
    `~/.claude/projects/`. Nothing in either payload names its own runtime, and
    the control plane needs to know: with a Claude driver and a Codex driver
    both configured, a card that cannot say which one it came from goes to
    neither and lands in the default chat.
    """
    path = str(transcript_path or "")
    if "/.codex/" in path:
        return "codex"
    if "/antigravity/" in path or "/.gemini/" in path:
        return "antigravity"
    return "claude-code"


def codex_thread_name(session_id: str | None, home: Path | None = None) -> str | None:
    """A Codex session's name, which is not in its transcript.

    Claude Code writes the title into the conversation; Codex keeps it in
    `~/.codex/session_index.jsonl`, appended, so the last line for an id wins.
    Reading it here rather than in core keeps the bridge able to answer the
    only question routing asks — which seat is this — with the stdlib alone.

    Fails quietly. No name simply means no seat, and everything lands in the
    default chat exactly as it would have anyway.
    """
    if not session_id:
        return None
    index = (home or Path.home() / ".codex") / "session_index.jsonl"
    found = None
    try:
        with index.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if session_id not in line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("id") == session_id and record.get("thread_name"):
                    found = str(record["thread_name"])
    except OSError:
        return None
    return found


def session_name(transcript_path: str | None) -> str | None:
    """The name shown on a session in the desktop app, if it can be found.

    Two Claude Code sessions on one codebase are indistinguishable to a hook
    except by `session_id`, which is a fresh UUID every time a session is
    resumed — useless for saying "this one is the navigator". The *name* is not:
    the same title was observed across three different session ids belonging to
    one named conversation.

    There is no name in the hook payload, so it is read out of the transcript,
    which the payload does point at. The transcript format is documented as
    internal and liable to change between releases, so this is written to fail
    quietly: no name simply means no routing, and everything lands in the
    default chat exactly as it would have anyway.

    A title the user set wins over one Claude generated, because the generated
    one changes as the conversation moves and the point of this is to be stable.
    """
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None

    custom = generated = None
    for raw in tail.split(b"\n"):
        if b"-title" not in raw:
            continue
        try:
            record = json.loads(raw)
        except Exception:
            continue
        if record.get("type") == "custom-title" and record.get("customTitle"):
            custom = str(record["customTitle"])
        elif record.get("type") == "ai-title" and record.get("aiTitle"):
            generated = str(record["aiTitle"])
    return custom or generated


def note(message: str) -> None:
    """Append one line to the bridge log, and never fail because of it.

    Wrapped completely: a gate that stops working because its log file is
    unwritable would be a worse failure than the one this is here to diagnose.
    """
    try:
        with BRIDGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass
