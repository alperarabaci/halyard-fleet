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


def _has_reset(resets_at: object, now: float) -> bool:
    """Whether this window's allowance has already been given back.

    Judged on the reset time rather than on how old the line is, which is the
    distinction that matters: a weekly reading from this morning is still true
    this evening, and a five-hour reading from this morning is not.

    A reading with no usable reset time is treated as current. Nothing here can
    tell, and silence is the failure this watcher exists to prevent.
    """
    if not isinstance(resets_at, int | float) or resets_at <= 0:
        return False
    return float(resets_at) < now


def _reset_wording(resets_at: object, now: float) -> str:
    """ "resets 19:30", or " it reset at 15:52", or nothing if unsaid.

    Tense, because the same timestamp means two different things. Measured on a
    real message: "is at 99% of its 5h Codex limit, resets 15:52" arrived at
    18:48, and every word of it was true about a window that had ended three
    hours earlier. Read on a phone it says the limit is nearly full *now* and
    the reset is somehow in the past — two wrong impressions from one honest
    sentence in the wrong tense.
    """
    if not isinstance(resets_at, int | float) or resets_at <= 0:
        return ""
    try:
        when = datetime.fromtimestamp(float(resets_at))
    except (OverflowError, OSError, ValueError):
        return ""
    if _has_reset(resets_at, now):
        return f"; it reset at {when.strftime('%H:%M')}"
    return f", resets {when.strftime('%H:%M')}"


def _readings(lines: Iterable[str]) -> list[dict]:
    """Every `rate_limits` in these lines, oldest first."""
    found: list[dict] = []
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
            found.append(limits)
    return found


def _last_reading(lines: Iterable[str]) -> dict | None:
    """The newest reading, which is the one that describes now.

    What `/status` wants: somebody asking where a seat stands is asking about
    this moment, and the nineteen readings before it are history.
    """
    found = _readings(lines)
    return found[-1] if found else None


def _highest_reading(lines: Iterable[str]) -> dict | None:
    """The fullest each window got across these lines.

    What an *alert* wants, which is a different question, and getting it wrong
    cost two silent exhaustions. A window is written on every turn, so a poll
    catching up on twenty turns can hold a reading of 100% followed by one of
    98% — the limit was reached, the window then rolled over, and by the time
    anybody looked the newest number was below the threshold. Measured on a
    real machine: the rollout held 100.0 and the last reading was 98.0, and the
    warning that a limit had been reached was never sent. Twice.

    Taking the peak is safe because saying it twice is already prevented: the
    key carries the window, the threshold and the reset time, so a threshold
    crossed in one batch and again in the next is one message, and the same
    threshold in a *new* window is genuinely new.

    Composed per window rather than per reading. The two windows fill at
    different rates and there is no reason the same turn holds the peak of
    both.
    """
    found = _readings(lines)
    if not found:
        return None
    highest: dict = {}
    for reading in found:
        for which in ("primary", "secondary"):
            window = reading.get(which)
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            if not isinstance(used, int | float):
                continue
            standing = highest.get(which)
            if standing is None or used > standing.get("used_percent", -1):
                highest[which] = window
    return highest or None


def alerts(lines: Iterable[str], seen: set[str], now: float | None = None) -> list[Alert]:
    """What is worth saying about the usage windows in these lines.

    The *highest* reading in a batch, per window. A limit reached between two
    polls and rolled over before the next one is still a limit that was
    reached, and taking the newest number missed exactly that — twice, in the
    same week, on the same machine.

    Which means a peak whose window has since closed is reported on purpose,
    and it has to be reported *as* that. Said in the present tense it claims
    the seat is nearly out of allowance right now, when the allowance has been
    back for hours — and it pins that claim to a reset time already in the
    past, which is how somebody spotted it. So the tense follows the clock.

    The key carries the window, the threshold and the reset time, so each is
    said once per window and again after it rolls over — a window that has
    reset is a new fact, not a repeat.
    """
    latest = _highest_reading(lines)
    if latest is None:
        return []
    if now is None:
        now = datetime.now().timestamp()

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
        over = _has_reset(resets, now)
        # The higher threshold first, so a window that jumped straight past both
        # says the useful thing rather than the earlier one.
        for threshold, phrasing in (
            (
                FULL_AT,
                f"used its whole {name} Codex limit"
                if over
                else f"has used its whole {name} Codex limit",
            ),
            (
                WARN_AT,
                f"reached {used:.0f}% of its {name} Codex limit"
                if over
                else f"is at {used:.0f}% of its {name} Codex limit",
            ),
        ):
            if used < threshold:
                continue
            key = f"{which}:{threshold}:{resets}"
            if key in seen:
                break
            # Nor anything milder, once the worse thing has been said. A window
            # reported full and then read at 99% would otherwise announce the
            # 90% mark it had skipped on the way up — "is at 99%" arriving
            # after "has used its whole limit", which reads as the limit
            # un-filling itself.
            if any(
                f"{which}:{higher}:{resets}" in seen for higher in (FULL_AT,) if higher > threshold
            ):
                break
            found.append(Alert(key=key, text=f"{phrasing}{_reset_wording(resets, now)}."))
            break
    return found


def usage(lines: Iterable[str]) -> tuple[str, ...]:
    """How full each window is right now, in a few words.

    The same last-reading rule as `alerts`, and for the same reason: the event
    is written every turn, so a file holds hundreds of readings and only the
    newest is true.

    Every window, not only the alarming ones. This is asked by somebody who
    wants to know where they stand — "45% of the weekly" is the answer to that
    question, and saying nothing until it is nearly full is the answer to a
    different one.
    """
    latest = _last_reading(lines)
    if latest is None:
        return ()
    said: list[str] = []
    for which in ("primary", "secondary"):
        window = latest.get(which)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if not isinstance(used, int | float):
            continue
        said.append(f"{_window_name(window.get('window_minutes'))} {used:.0f}%")
    return tuple(said)


WATCHING = Watching(home=home(), transcript=transcript, alerts=alerts, usage=usage)
