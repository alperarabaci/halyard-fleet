"""Adding and removing the gate, without destroying anything on the way.

`.claude/settings.local.json` is not Halyard's file. Claude Code writes to it
too — every "don't ask again" appends a rule to a `permissions.allow` list that
lives there — and the file is gitignored, so nothing keeps a copy but you.

The README used to say "put this JSON in that file", showing a document with
only `hooks` in it. Followed literally, that deletes the permission list. It
happened, on 2026-07-22, and the symptom was not an error: approvals kept
working and a session simply started asking again about commands it had settled
months earlier. Nobody connects that to a config edit from days before.

So wiring is a merge, unwiring removes only what this install put there, and
both take a copy first.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"

#: `PreToolUse` is the gate. `Stop` is the relay that sends replies to a phone.
WIRING = (
    ("PreToolUse", "Bash", BRIDGE_DIR / "hook.sh", 600),
    ("Stop", None, BRIDGE_DIR / "relay.py", 15),
)

# Codex asks a separate native question when a tool requests sandbox
# escalation. A PreToolUse allow does not answer it. This stays Codex-only:
# Claude Code's PreToolUse decision already replaces its permission prompt.
CODEX_WIRING = (("PermissionRequest", "Bash", BRIDGE_DIR / "permission_hook.sh", 600),)

# Antigravity-only, and it is how a message gets in at all. `PreInvocation` is
# the one hook that can answer with `injectSteps`, and a `{"userMessage": ...}`
# step is the only way text enters a conversation as a turn the person typed —
# `agentapi send-message` can file a SYSTEM_MESSAGE and nothing else. The other
# two runtimes have a CLI that resumes a session and need none of this.
ANTIGRAVITY_WIRING = (("PreInvocation", None, BRIDGE_DIR / "inject.py", 15),)


@dataclass(frozen=True)
class Runtime:
    """One runtime's idea of where hooks live and how a tool is matched.

    The shape of a hook entry is the same in both — an event, a list of groups,
    a `command` and a `timeout` — which is why one merge routine serves both.
    What differs is the file and the matcher dialect.
    """

    name: str
    #: Relative to the repository root.
    settings: str
    matcher: str
    #: The CLI whose presence means this runtime is worth wiring at all.
    binary: str
    #: Whether this runtime is on the machine, for when a PATH lookup is not
    #: the answer. Antigravity's application bundles its binaries inside the
    #: `.app` and puts nothing on PATH, so `which` reports it missing on a
    #: machine where it is certainly installed — and the gate would then be
    #: skipped on the only runtime that cannot be reached any other way.
    present: Callable[[], bool] | None = None

    def on_this_machine(self) -> bool:
        return self.present() if self.present else bool(shutil.which(self.binary))


RUNTIMES = (
    Runtime(
        name="claude-code",
        settings=".claude/settings.local.json",
        matcher="Bash",
        binary="claude",
    ),
    # Codex matches with a regular expression, and with more than one name.
    #
    # `codex exec` runs a shell command as a tool the hook payload calls
    # `Bash`. The desktop app does not: it calls a tool named `exec` whose
    # input is JavaScript, and the shell call happens inside that —
    # `tools.exec_command({"cmd": "git reset", ...})`. A matcher of `^Bash$`
    # therefore gates the CLI and silently ignores the app, which is a gate
    # that looks installed, reports itself installed, and never fires.
    #
    # Measured from a real transcript: the same session, driven both ways, made
    # a `function_call` named `exec_command` from the CLI and a
    # `custom_tool_call` named `exec` from the app.
    Runtime(
        name="codex",
        settings=".codex/hooks.json",
        matcher="^(Bash|exec|exec_command|shell)$",
        binary="codex",
    ),
    # Antigravity names its shell tool `run_command`, and its matcher is a
    # regex over tool names derived by "lowercasing the step type and removing
    # the CORTEX_STEP_TYPE_ prefix".
    Runtime(
        name="antigravity",
        settings=".agents/hooks.json",
        matcher="run_command",
        binary="agy",
        present=lambda: _antigravity_present(),
    ),
)

#: The top-level key this install writes its hooks under.
#:
#: Antigravity's file is not `{"hooks": {...}}` like the other two. Each
#: top-level key is a *hook name* whose value holds that hook's events, and
#: every name contributing to an event is merged and run in turn. So this is a
#: namespace rather than a wrapper, and two tools can gate the same project
#: without either one having to know about the other.
ANTIGRAVITY_NAME = "halyard"

#: The events Antigravity wraps in a `matcher`/`hooks` group. Every other event
#: — `Stop` among them — is a *flat* list of handler objects.
#:
#: Not cosmetic. A `Stop` handler written in the grouped shape has no `command`
#: where Antigravity looks for one, so the relay is not run and no reply ever
#: reaches a phone — with the file present, and everything reporting wired.
ANTIGRAVITY_GROUPED = ("PreToolUse", "PostToolUse")


def _antigravity_present() -> bool:
    """The application or its CLI, either of which means this is worth wiring."""
    from halyard.agents.antigravity import find_antigravity_binary

    return find_antigravity_binary() is not None or bool(shutil.which("agy"))


RULES = """\
Three things to know before you walk away from this machine:

  1. This project now needs the control plane running. While the hook is
     wired, a Bash command with Halyard down is DENIED — every one of them,
     including `ls`. `halyard unwire` puts the project back.

  2. It is live as soon as it starts. Approvals go to Telegram from the
     first command; there is no arming step. `/pause` is what stops it, and
     pausing needs the server running too.

  3. An approval expires. Nobody answers within the approval timeout and it
     is denied, not left waiting.
"""


def project_root(directory: Path) -> Path:
    """Where Claude Code looks for `.claude/`: the repository root.

    Measured — a session opened in a subdirectory picks up hooks from the top
    of its repository, and picks up nothing at all when there is no repository
    above it.
    """
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory


def settings_path(directory: Path, runtime: Runtime | None = None) -> Path:
    return project_root(directory) / (runtime or RUNTIMES[0]).settings


def installed(runtimes: tuple[Runtime, ...] = RUNTIMES) -> tuple[Runtime, ...]:
    """The runtimes whose CLI is on this machine.

    Wiring is offered for these and removal is attempted for all of them. The
    asymmetry is deliberate: adding a gate for a runtime nobody has is clutter,
    while leaving one behind after a CLI is uninstalled is a hook pointing at a
    bridge nothing will ever call.
    """
    return tuple(r for r in runtimes if r.on_this_machine())


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


def codex_trust_keys(hooks_file: Path) -> list[str]:
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


def codex_untrusted(hooks_file: Path, config_toml: Path | None = None) -> list[str]:
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
    return [key for key in codex_trust_keys(hooks_file) if f'"{key}"' not in recorded]


def codex_trust_is_stale(hooks_file: Path, config_toml: Path | None = None) -> bool:
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


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(f"halyard: {path} is not valid JSON ({error}). Not touching it.") from None
    if not isinstance(loaded, dict):
        raise SystemExit(f"halyard: {path} does not contain a JSON object. Not touching it.")
    return loaded


def _back_up(path: Path) -> Path | None:
    """Copy the file aside before writing it.

    Timestamped rather than a single `.bak`, because the mistake worth
    protecting against is running this twice — a fixed name would overwrite the
    good copy with the already-damaged one on the second run.
    """
    if not path.exists():
        return None
    # Microseconds keep two material rewrites in the same second from reusing
    # one path. A backup operation must never overwrite an earlier backup:
    # that earlier copy may be the only version containing a lost allowlist.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def _write(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _is_ours(command: object) -> bool:
    """Whether this hook entry is one this install put there.

    Path-based, so unwiring cannot remove somebody else's hook — including the
    same script from a second Halyard checkout, which is a real shape: two
    machines sharing a settings file through a synced directory.
    """
    return isinstance(command, str) and str(BRIDGE_DIR.resolve()) in command


def antigravity_handlers(spec: dict, event: str) -> list[dict]:
    """Every handler an Antigravity hook spec has for one event.

    Flattens the two shapes so callers can ask one question. Reading a grouped
    event as flat yields the group — an object with a `matcher` and no
    `command` — which looks like a hook pointing nowhere.
    """
    entries = spec.get(event) or []
    if not isinstance(entries, list):
        return []
    if event in ANTIGRAVITY_GROUPED:
        return [h for group in entries for h in (group or {}).get("hooks") or []]
    return [entry for entry in entries if isinstance(entry, dict)]


def _wire_antigravity(directory: Path, runtime: Runtime) -> int:
    """Add the gate to `.agents/hooks.json`, in Antigravity's own shape."""
    path = settings_path(directory, runtime)
    config = _load(path)
    spec = config.setdefault(ANTIGRAVITY_NAME, {})
    if not isinstance(spec, dict):
        raise SystemExit(f"halyard: {path} has a {ANTIGRAVITY_NAME!r} that is not an object.")

    added = []
    for event, _matcher, script, timeout in WIRING + ANTIGRAVITY_WIRING:
        if not script.exists():
            print(f"halyard: {script} is missing from this install")
            return 1
        if any(_is_ours(h.get("command")) for h in antigravity_handlers(spec, event)):
            continue
        handler = {"type": "command", "command": str(script), "timeout": timeout}
        entries = spec.setdefault(event, [])
        entries.append(
            {"matcher": runtime.matcher, "hooks": [handler]}
            if event in ANTIGRAVITY_GROUPED
            else handler
        )
        added.append(event)

    # `enabled: false` turns off every handler under this name while leaving
    # the file looking exactly like a wired one. Wiring means the gate works,
    # so it is turned back on — and said out loud, because somebody put it
    # there on purpose and deserves to know it was undone.
    re_enabled = spec.get("enabled") is False
    if re_enabled:
        spec["enabled"] = True

    if not added and not re_enabled:
        print(f"  {runtime.name}: already wired ({path})")
        return 0

    backup = _back_up(path)
    _write(path, config)
    if added:
        print(f"  {runtime.name}: wired {', '.join(added)} into {path}")
    if re_enabled:
        print(f'  {runtime.name}: "enabled": false was turned back on — it disabled the gate')
    if backup:
        print(f"    previous version kept at {backup}")
    kept = sorted(k for k in config if k != ANTIGRAVITY_NAME)
    if kept:
        print(f"    left untouched in that file: {', '.join(kept)}")
    return 0


def _unwire_antigravity(directory: Path, runtime: Runtime) -> int:
    path = settings_path(directory, runtime)
    if not path.exists():
        return 0
    config = _load(path)
    spec = config.get(ANTIGRAVITY_NAME)
    if not isinstance(spec, dict):
        return 0

    removed = []
    for event in [key for key in spec if key != "enabled"]:
        entries = spec.get(event) or []
        if event in ANTIGRAVITY_GROUPED:
            kept = []
            for group in entries:
                before = (group or {}).get("hooks") or []
                mine = [h for h in before if not _is_ours(h.get("command"))]
                if len(mine) != len(before):
                    removed.append(event)
                if mine:
                    kept.append({**group, "hooks": mine})
        else:
            kept = [h for h in entries if not _is_ours((h or {}).get("command"))]
            if len(kept) != len(entries):
                removed.append(event)
        if kept:
            spec[event] = kept
        else:
            del spec[event]

    if not removed:
        return 0
    # A name holding nothing but `enabled` is not a hook, so the whole key goes
    # rather than being left as a husk somebody has to work out the meaning of.
    if not [key for key in spec if key != "enabled"]:
        config.pop(ANTIGRAVITY_NAME, None)

    backup = _back_up(path)
    _write(path, config)
    print(f"  {runtime.name}: removed {', '.join(sorted(set(removed)))} from {path}")
    if backup:
        print(f"    previous version kept at {backup}")
    kept_keys = sorted(k for k in config if k != ANTIGRAVITY_NAME)
    if kept_keys:
        print(f"    left untouched in that file: {', '.join(kept_keys)}")
    return 1


def _wire_one(directory: Path, runtime: Runtime) -> int:
    """Add one runtime's hooks, keeping everything already in its file."""
    if runtime.name == "antigravity":
        return _wire_antigravity(directory, runtime)
    path = settings_path(directory, runtime)
    config = _load(path)
    hooks = config.setdefault("hooks", {})

    added = []
    runtime_wiring = WIRING + (CODEX_WIRING if runtime.name == "codex" else ())
    for event, matcher, script, timeout in runtime_wiring:
        if not script.exists():
            print(f"halyard: {script} is missing from this install")
            return 1
        groups = hooks.setdefault(event, [])
        mine = [
            group
            for group in groups
            for hook in (group or {}).get("hooks") or []
            if _is_ours(hook.get("command"))
        ]
        if mine:
            # Already wired, but possibly with a matcher from an older release.
            # Leaving a stale one in place is the quiet kind of wrong: the file
            # is present, `doctor` is happy, and half the tool calls are not
            # gated. Correcting it costs a re-review of the hook, which is the
            # honest price of the matcher having been incomplete.
            wanted = runtime.matcher if matcher == "Bash" else matcher
            for group in mine:
                if wanted and group.get("matcher") != wanted:
                    group["matcher"] = wanted
                    added.append(f"{event} (matcher corrected)")
            continue
        # An absolute path, always. Claude Code expands `$CLAUDE_PROJECT_DIR`
        # and Codex expands nothing of the kind — it has no project variable at
        # all, only `$CODEX_HOME`. A hooks file written with the Claude
        # variable in it does not fail to load under Codex; the hook runs and
        # dies looking for a directory called `$CLAUDE_PROJECT_DIR`, which is
        # what "hook: Stop Failed" meant when this repository's own file had it.
        entry: dict = {"hooks": [{"type": "command", "command": str(script), "timeout": timeout}]}
        if matcher:
            entry["matcher"] = runtime.matcher if matcher == "Bash" else matcher
        groups.append(entry)
        added.append(event)

    if not added:
        print(f"  {runtime.name}: already wired ({path})")
        return 0

    backup = _back_up(path)
    _write(path, config)
    print(f"  {runtime.name}: wired {', '.join(added)} into {path}")
    if backup:
        print(f"    previous version kept at {backup}")
    kept = sorted(k for k in config if k != "hooks")
    if kept:
        # Say it out loud. Losing this silently is the failure this whole
        # module exists to prevent, and "nothing was reported" is not the
        # same reassurance as "your permissions are still there".
        print(f"    left untouched in that file: {', '.join(kept)}")
    return 0


def wire(directory: Path, runtimes: tuple[Runtime, ...] | None = None) -> int:
    """Put the gate on a project, for every runtime this machine has.

    Wiring a runtime the machine does not have would be clutter, so the default
    is what is installed — falling back to the first entry when nothing is
    found, because a PATH that hides the CLI is a likelier explanation than a
    machine with no agent on it at all.
    """
    chosen = runtimes if runtimes is not None else installed() or RUNTIMES[:1]
    print(f"Wiring {project_root(directory)}")
    for runtime in chosen:
        if _wire_one(directory, runtime):
            return 1

    for runtime in chosen:
        if runtime.name != "codex":
            continue
        hooks_file = settings_path(directory, runtime)
        pending = codex_untrusted(hooks_file) or (
            codex_trust_keys(hooks_file) if codex_trust_is_stale(hooks_file) else []
        )
        if pending:
            # Loud, because the failure it prevents is silent. Codex skips an
            # untrusted hook without a word, so a Codex project can look wired,
            # report wired, and have no gate on it at all.
            print(
                f"\n⚠ {len(pending)} Codex hook(s) here are not trusted yet, and Codex "
                "SKIPS\n"
                "  an untrusted hook without saying so — a gate that is not trusted is\n"
                "  not a gate. Open this project in Codex once and approve them, then\n"
                "  check with `halyard doctor`."
            )

    print(f"\nRestart the session — hooks are read at startup.\n\n{RULES}")
    return 0


def _unwire_one(directory: Path, runtime: Runtime) -> int:
    """Remove only this install's hooks, and nothing else."""
    if runtime.name == "antigravity":
        return _unwire_antigravity(directory, runtime)
    path = settings_path(directory, runtime)
    if not path.exists():
        return 0

    config = _load(path)
    hooks = config.get("hooks") or {}
    removed = []
    for event in list(hooks):
        groups = hooks.get(event) or []
        kept_groups = []
        for group in groups:
            before = (group or {}).get("hooks") or []
            entries = [h for h in before if not _is_ours(h.get("command"))]
            if len(entries) != len(before):
                removed.append(event)
            if entries:
                kept_groups.append({**group, "hooks": entries})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        config.pop("hooks", None)

    if not removed:
        return 0

    backup = _back_up(path)
    _write(path, config)
    print(f"  {runtime.name}: removed {', '.join(sorted(set(removed)))} from {path}")
    if backup:
        print(f"    previous version kept at {backup}")
    kept = sorted(k for k in config if k != "hooks")
    if kept:
        print(f"    left untouched in that file: {', '.join(kept)}")
    return 1


def unwire(directory: Path, runtimes: tuple[Runtime, ...] | None = None) -> int:
    """Take the gate off, wherever it was put.

    Every runtime by default, not just the installed ones: a hook left behind
    after a CLI was removed still points at a bridge, and the next person to
    install that CLI inherits a gate they never asked for.
    """
    chosen = runtimes if runtimes is not None else RUNTIMES
    touched = sum(_unwire_one(directory, runtime) for runtime in chosen)
    if not touched:
        print(f"Nothing of this Halyard install is wired into {project_root(directory)}")
        return 0
    print("\nRestart the session — hooks are read at startup.")
    print("Bash no longer goes through Halyard in this project.")
    return 0
