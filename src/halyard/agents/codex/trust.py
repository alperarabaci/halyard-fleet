"""Whether Codex will actually run the hooks a project has.

Codex will not run a hook it has not been told to trust, and — measured — it
does not say so. An untrusted hook is skipped in silence: the turn completes
normally, nothing is printed, and for a `PreToolUse` gate that means there is
no gate at all while everything looks wired.

None of this is a question the other runtimes have. It lived in `wiring.py` and
`doctor.py` as two `if runtime == "codex"` blocks, which is how a module about
merging JSON ended up knowing where Codex keeps its config file.
"""

from __future__ import annotations

import json
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "bridge"


def _snake(event: str) -> str:
    """`PreToolUse` as Codex writes it in a trust key: `pre_tool_use`.

    Lowercasing alone gives `pretooluse`, which matches nothing — so every hook
    read as never trusted, and doctor reported no gate on a project that had
    one. A checker that is confidently wrong is worse than no checker: the
    obvious response to its FAIL is to go and re-grant trust that was never
    missing.
    """
    out = []
    for index, character in enumerate(event):
        if character.isupper() and index:
            out.append("_")
        out.append(character.lower())
    return "".join(out)


def trust_keys(hooks_file: Path) -> list[str]:
    """The trust keys Codex would look for, one per hook entry in that file.

    Codex will not run a hook it has not been told to trust, and — measured —
    it does not say so. An untrusted hook is skipped in silence: the turn
    completes normally, no warning is printed, and for a `PreToolUse` gate that
    means there is no gate at all while everything looks wired.

    Trust is recorded in `~/.codex/config.toml` under
    `[hooks.state."<file>:<event>:<group>:<hook>"]`, each with a
    `trusted_hash`. The hash covers the entry, so editing a command invalidates
    it — which is how this repository's own relay stopped firing the moment its
    path was corrected.
    """
    try:
        config = json.loads(hooks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    keys = []
    for event, groups in (config.get("hooks") or {}).items():
        for group_index, group in enumerate(groups if isinstance(groups, list) else []):
            for hook_index, _ in enumerate((group or {}).get("hooks") or []):
                keys.append(f"{hooks_file}:{_snake(event)}:{group_index}:{hook_index}")
    return keys


def untrusted(hooks_file: Path, config_toml: Path | None = None) -> list[str]:
    """Hook entries with no trust record at all — Codex will skip these.

    Only absence is reported. Whether a record that *does* exist still matches
    is a question about Codex's own hashing, which is not reimplemented here:
    guessing at somebody else's canonicalisation would produce a checker that
    is confidently wrong, which is worse than one that states its limit.
    """
    toml = config_toml or Path.home() / ".codex" / "config.toml"
    try:
        recorded = toml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        recorded = ""
    return [key for key in trust_keys(hooks_file) if f'"{key}"' not in recorded]


def is_stale(hooks_file: Path, config_toml: Path | None = None) -> bool:
    """Whether anything trust covers has changed since it was last recorded.

    A one-directional inference, and sound in the direction it is made: Codex
    writes trust into `config.toml`, so a hooks file modified *after* that file
    means no trust has been recorded since the edit, and the entry's hash
    cannot still match. The reverse says nothing — `config.toml` is rewritten
    for unrelated reasons — so this reports staleness and never freshness.

    Worth having because the alternative reading is the dangerous one. A trust
    key that still exists with an outdated hash looks exactly like a trusted
    hook, and Codex skips it in silence.
    """
    toml = config_toml or Path.home() / ".codex" / "config.toml"
    # The scripts as well as the file that names them. Codex records a SHA-256
    # of the handler, so updating this checkout — a `git pull` that touches
    # `hook.sh` — plausibly revokes trust on every project it is wired into,
    # silently, in the way everything about Codex hook trust is silent.
    #
    # Unverified: the exact input to that hash is not reimplemented here, for
    # the reason given above. Watching the scripts as well as the file costs a
    # warning that is sometimes unnecessary, against missing one that means a
    # gate has disappeared.
    watched = [
        hooks_file,
        BRIDGE_DIR / "hook.sh",
        BRIDGE_DIR / "permission_hook.sh",
        BRIDGE_DIR / "relay.py",
    ]
    try:
        recorded = toml.stat().st_mtime
        return any(path.stat().st_mtime > recorded for path in watched if path.exists())
    except OSError:
        return False


def check_wired(hooks_file: Path, project_dir: Path) -> list[tuple[str, str]]:
    """Everything that can make a wired Codex project have no gate.

    Two failures, both silent, both measured. Codex accepts `description` and
    `hooks` and refuses a file containing anything else — the whole file, not
    the offending field — with a warning on stderr and then behaves as though
    no hooks existed. And a hook nobody has trusted is skipped without a word.

    Returned as `(level, text)` for the caller to render, so the same answer
    serves `doctor`'s report and `wire`'s closing warning.
    """
    try:
        keys = set(json.loads(hooks_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        keys = set()

    findings: list[tuple[str, str]] = []
    stray = sorted(keys - {"description", "hooks"})
    if stray:
        findings.append(("fail", f"hooks.json has field(s) Codex rejects: {', '.join(stray)}"))
        findings.append(("", "it refuses the whole file over one, so nothing is gated"))

    if untrusted(hooks_file):
        findings.append(("fail", "hooks here have never been trusted"))
        findings.append(("", "Codex skips an untrusted hook without a word, so this"))
        findings.append(("", "project has no gate at all. Review and trust them:"))
        findings.append(("", f"    cd {project_dir} && codex"))
        findings.append(("", "A hook review appears at startup. Prefer `Review hooks`"))
        findings.append(("", "over `Trust all` — these run from outside the project."))
    elif is_stale(hooks_file):
        findings.append(("warn", "hooks or bridge scripts changed since trust was recorded"))
        findings.append(("", "Codex hashes the handler, so updating this checkout may"))
        findings.append(("", "have revoked it — and a revoked hook is skipped in"))
        findings.append(("", f"silence. Re-review with: cd {project_dir} && codex"))
    else:
        # Existence is all this can prove without reimplementing Codex's
        # private hash canonicalisation. Say exactly that: it explains why no
        # review prompt appeared without claiming more than the persisted
        # state establishes.
        findings.append(("ok", "trust records exist for every Codex hook"))
    return findings
