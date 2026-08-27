"""How full Codex's usage windows are, which it tells nobody outside its own UI.

Codex has a five-hour limit and a weekly one, and hitting either stops work
without anything reaching a phone. What it does do is write its own accounting
into the rollout on every turn — a `token_count` event carrying `rate_limits`.
Measured on a live rollout:

    "primary":   {"used_percent": 91.0, "window_minutes": 300,   "resets_at": …}
    "secondary": {"used_percent": 45.0, "window_minutes": 10080, "resets_at": …}
    "plan_type": "plus"

That is a better position than the Claude side of this, where only the wreckage
is visible after the fact. A percentage that climbs on every turn can be
reported *before* the wall rather than after it — which is the difference
between "leave now if you want to finish this" and "you have already stopped".

So this warns as a window fills and again when it is full, each once per window,
and says when it opens again.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from halyard.agents.spec import Alert, Watching

#: Where a window is worth mentioning, and where it is worth an alarm. Two
#: thresholds rather than one: the first is the message that lets somebody
#: change what they are doing, and the second is the one that explains why
#: everything stopped.
WARN_AT = 90.0
FULL_AT = 100.0


#: What to call each window. Codex names them `primary` and `secondary`, which
#: says nothing on a phone; the length does.
def _window_name(minutes: object) -> str:
    if not isinstance(minutes, int | float) or minutes <= 0:
        return "usage"
    if minutes % 1440 == 0:
        days = int(minutes // 1440)
        return "weekly" if days == 7 else f"{days}-day"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes)}m"


def home() -> Path:
    """Where Codex keeps its rollouts."""
    return Path.home() / ".codex"


def transcript(session_id: str, root: Path) -> Path | None:
    """This session's rollout, by id.

    Codex files its rollouts by date and names them
    `rollout-<timestamp>-<session_id>.jsonl`, so the id is a suffix rather than
    the whole name — which is why this is the runtime's own business and not
    something core could have guessed.
    """
    try:
        for found in root.glob(f"sessions/**/rollout-*-{session_id}.jsonl"):
            if found.is_file():
                return found
    except OSError:
        return None
    return None


def _reset_wording(resets_at: object) -> str:
    """ "resets 19:30", or nothing if the runtime did not say."""
    if not isinstance(resets_at, int | float) or resets_at <= 0:
        return ""
    try:
        when = datetime.fromtimestamp(float(resets_at))
    except (OverflowError, OSError, ValueError):
        return ""
    return f", resets {when.strftime('%H:%M')}"


def alerts(lines: Iterable[str], seen: set[str]) -> list[Alert]:
    """What is worth saying about the usage windows in these lines.

    Only the *last* reading in a batch is considered. The event is written on
    every turn, so a poll that catches up on twenty of them holds twenty
    readings of the same window, and only the newest is true.

    The key carries the window, the threshold and the reset time, so each is
    said once per window and again after it rolls over — a window that has
    reset is a new fact, not a repeat.
    """
    latest: dict | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        limits = (
            (entry.get("payload") or {}).get("rate_limits") if isinstance(entry, dict) else None
        )
        if isinstance(limits, dict):
            latest = limits
    if latest is None:
        return []

    found: list[Alert] = []
    for which in ("primary", "secondary"):
        window = latest.get(which)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if not isinstance(used, int | float):
            continue
        name = _window_name(window.get("window_minutes"))
        resets = window.get("resets_at")
        # The higher threshold first, so a window that jumped straight past both
        # says the useful thing rather than the earlier one.
        for threshold, phrasing in (
            (FULL_AT, f"has used its whole {name} Codex limit"),
            (WARN_AT, f"is at {used:.0f}% of its {name} Codex limit"),
        ):
            if used < threshold:
                continue
            key = f"{which}:{threshold}:{resets}"
            if key in seen:
                break
            found.append(Alert(key=key, text=f"{phrasing}{_reset_wording(resets)}."))
            break
    return found


WATCHING = Watching(home=home(), transcript=transcript, alerts=alerts)
