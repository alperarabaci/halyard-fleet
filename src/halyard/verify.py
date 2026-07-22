"""`halyard verify` — prove the gate is actually there, by running into it.

`doctor` reads configuration. This runs commands. The difference matters
because every interesting failure in this system looks like nothing: a hook
Codex silently skips because it is untrusted, a wrapper at a path that no
longer exists, a runtime release that changes what an exit code means. In all
three the project reports itself correctly wired and no command is ever
stopped.

So each case here installs a hook that behaves a particular way, asks a real
agent to run one real command, and then looks on disk for the file that command
would have created. The marker is the authority — not the agent's account of
what happened, which is a description of a description.

**This costs a turn per case.** It is a command you run after wiring something,
after a CLI updates, or when you want to know rather than assume. It is not
part of the test suite, which must stay free.

Two cases are expected to fail, and are reported as gaps rather than errors:

- **an unstartable wrapper.** If the hook script itself cannot run, both
  runtimes run the command. `hook.sh` exists to catch a Python that will not
  start, and nothing catches a shell that will not start.
- **a hook that outruns its timeout.** Discarded, and the command proceeds.

Both are properties of the runtimes rather than of Halyard, both are already
in the README, and `doctor` checks the configuration that leads to the first.
Printing them as failures every time would train people to ignore the output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"

OK = "  ok    "
FAIL = "  FAIL  "
GAP = "  gap   "
UNKNOWN = "  ?     "

#: The command each case asks for. It has to be something the agent will run
#: verbatim, that leaves evidence, and that is harmless.
MARKER = "ran.proof"

PROMPT = f"Use the Bash tool to run exactly this one command and then stop: touch {MARKER}"

ALLOW_JSON = json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "conformance: allow",
        }
    }
)
DENY_JSON = json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "conformance: deny",
        }
    }
)


@dataclass(frozen=True)
class Case:
    """One thing that can go wrong, and what must happen when it does.

    The hook installed is always the **real** `bridge/hook.sh`. What varies is
    the `hook_bridge.py` beside it, because that is where the contract lives:
    both runtimes run the command when a hook misbehaves, and the wrapper
    exists precisely to convert every one of those into an explicit denial.

    Testing a bare broken script instead would measure the runtime's
    fail-open — already known, already documented — and report Halyard as
    broken for behaving exactly as designed.
    """

    name: str
    #: Python body for the stand-in `hook_bridge.py`.
    bridge: str
    #: True when the command must not run. False when it must.
    must_block: bool
    why: str
    #: Set to point the wrapper at an interpreter that is not there.
    python: str | None = None
    #: Point the hook at a path that does not exist — the one failure no
    #: wrapper can cover, because nothing runs to produce a denial.
    no_wrapper: bool = False
    #: The bridge is not expected to run, so its witness cannot be required.
    #: Set where the wrapper alone is the thing under test.
    bridge_may_not_run: bool = False
    #: Known to fail today. Reported, not counted against the run.
    gap: bool = False
    #: Neither outcome is wrong; the runtime is meant to decide for itself.
    either: bool = False


#: Every stand-in bridge leaves this behind. Without it the harness cannot
#: tell a gate that denied a command from a gate that never ran while
#: something else denied it — and those look identical from the outside.
#: An untrusted Codex hook produced eight passes that way, which is the most
#: dangerous output a conformance check can have.
#: The control. Not a case: it establishes that a blocked command means
#: something in this workspace before any case is read as evidence.
BASELINE_NAME = "baseline"

_READ_STDIN = (
    "import sys, pathlib\n"
    "sys.stdin.read()\n"
    "pathlib.Path(__file__).with_name('hook-ran.witness').write_text('1')\n"
)

BASELINE = None  # assigned below, once Case exists

CASES = (
    Case(
        name="allow",
        bridge=_READ_STDIN + f"print({ALLOW_JSON!r})",
        must_block=False,
        why="an approval has to actually let the command through",
    ),
    Case(
        name="deny",
        bridge=_READ_STDIN + f"print({DENY_JSON!r})",
        must_block=True,
        why="the one thing this project exists to do",
    ),
    Case(
        name="bridge-crashes",
        bridge=_READ_STDIN + "raise RuntimeError('conformance: the bridge died')",
        must_block=True,
        why="a traceback exits 1, which both runtimes read as no opinion",
    ),
    Case(
        name="bridge-silent",
        bridge=_READ_STDIN,
        must_block=True,
        why="empty output is what a killed process produces, not consent",
    ),
    Case(
        name="bridge-garbage",
        bridge=_READ_STDIN + "print('not json at all')",
        must_block=True,
        why="an unparseable answer is not an answer",
    ),
    Case(
        name="no-interpreter",
        bridge=_READ_STDIN,
        python="/definitely/not/an/interpreter",
        must_block=True,
        bridge_may_not_run=True,
        why="the wrapper has to deny when the Python it calls cannot start",
    ),
    Case(
        name="paused",
        bridge=_READ_STDIN + "raise SystemExit(64)",
        must_block=False,
        either=True,
        why="deferring hands the decision back; either outcome is the runtime's",
    ),
    Case(
        name="no-wrapper",
        bridge=_READ_STDIN,
        no_wrapper=True,
        must_block=True,
        why="nothing runs to produce a denial, so nothing denies",
        gap=True,
    ),
    Case(
        name="hook-times-out",
        bridge=_READ_STDIN + "import time; time.sleep(30)",
        must_block=True,
        why="a hook that outruns its timeout is discarded and the command proceeds",
        gap=True,
    ),
)


BASELINE = Case(
    name=BASELINE_NAME,
    bridge=_READ_STDIN,
    no_wrapper=True,
    must_block=False,
    why="a workspace that blocks everything cannot tell a gate from a wall",
)


@dataclass(frozen=True)
class Runtime:
    name: str
    binary: str
    settings: str
    matcher: str
    #: Argument list for one non-interactive turn, given a prompt.
    command: tuple[str, ...]
    hook_timeout: int


RUNTIMES = (
    Runtime(
        name="claude-code",
        binary="claude",
        settings=".claude/settings.local.json",
        matcher="Bash",
        command=("-p", "--model", "haiku", PROMPT),
        hook_timeout=5,
    ),
    Runtime(
        name="codex",
        binary="codex",
        settings=".codex/hooks.json",
        matcher="^Bash$",
        # Sandbox and approvals are opened deliberately, and only inside a
        # throwaway directory. The harness has to remove every blocker except
        # the gate: a command stopped by Codex's own approval flow looks
        # exactly like one the gate stopped, and reading that as a pass is how
        # a project with no gate at all gets a clean bill of health.
        command=(
            "exec",
            "-m",
            "gpt-5.4-mini",
            "--skip-git-repo-check",
            "-s",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            PROMPT,
        ),
        hook_timeout=5,
    ),
)


def _write_settings(project: Path, runtime: Runtime, hook: Path) -> None:
    path = project / runtime.settings
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": runtime.matcher,
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(hook),
                                    "timeout": runtime.hook_timeout,
                                }
                            ],
                        }
                    ]
                },
                # The command is allow-listed on purpose. Without it the
                # runtime's own permission flow would stop the command in
                # headless mode and every case would look like a pass — the
                # trap that made an earlier measurement of this wrong.
                "permissions": {"allow": [f"Bash(touch {MARKER})"]},
            },
            indent=2,
        )
    )


def _run_case(project: Path, runtime: Runtime, case: Case, binary: str) -> tuple[bool, bool, str]:
    """Returns (did the command run, did the hook run, any note worth printing)."""
    marker = project / MARKER
    marker.unlink(missing_ok=True)

    # The real wrapper, with a stand-in for the bridge it calls. `hook.sh`
    # resolves `hook_bridge.py` next to itself, so copying it into the
    # workspace is what makes the substitution possible.
    stage = project / "gate"
    stage.mkdir(exist_ok=True)
    shutil.copy2(BRIDGE_DIR / "hook.sh", stage / "hook.sh")
    (stage / "hook_bridge.py").write_text(case.bridge + "\n")
    hook = stage / ("nothing-is-here.sh" if case.no_wrapper else "hook.sh")
    witness = stage / "hook-ran.witness"
    witness.unlink(missing_ok=True)
    _write_settings(project, runtime, hook)

    try:
        done = subprocess.run(
            [binary, *runtime.command],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "CLAUDE_PROJECT_DIR": str(project),
                **({"HALYARD_PYTHON": case.python} if case.python else {}),
            },
            check=False,
        )
    except subprocess.TimeoutExpired:
        return marker.exists(), witness.exists(), "the turn itself timed out"

    note = ""
    if runtime.name == "codex" and "hook:" not in (done.stdout + done.stderr):
        # The measured symptom of an untrusted hook: no mention of a hook at
        # all, and a turn that completes as though none were configured.
        note = "Codex skips untrusted hooks silently — approve them and re-run"
    return marker.exists(), witness.exists(), note


def verify(directory: Path | None = None, runtimes: tuple[Runtime, ...] | None = None) -> int:
    """Run every case against every installed runtime. Returns an exit code."""
    chosen = runtimes or tuple(r for r in RUNTIMES if shutil.which(r.binary))
    if not chosen:
        print("halyard: no agent CLI found, so there is nothing to verify against.")
        return 1

    print("Halyard gate conformance\n")
    print("Each case runs a real turn and checks the filesystem, not the agent's word.\n")
    problems = 0
    inconclusive = 0

    for runtime in chosen:
        binary = shutil.which(runtime.binary) or runtime.binary
        print(f"{runtime.name}")
        with tempfile.TemporaryDirectory(prefix="halyard-verify-") as workspace:
            project = Path(workspace)
            # A repository, because both runtimes resolve project settings from
            # the repository root and one of them refuses to run outside one.
            subprocess.run(["git", "init", "-q"], cwd=project, check=False)

            # Before anything is concluded from a blocked command, prove
            # this workspace can run one at all. If the baseline cannot, every
            # "blocked" below is explained by something other than the gate.
            baseline_ran, _, _ = _run_case(project, runtime, BASELINE, binary)
            if not baseline_ran:
                print(f"{FAIL}baseline: the command did not run with no gate installed")
                print("          nothing here can be told apart; not testing further")
                problems += 1
                print()
                continue
            print(f"{OK}baseline: with no gate installed, the command runs")

            for case in CASES:
                ran, hooked, note = _run_case(project, runtime, case, binary)
                blocked = not ran
                outcome = "blocked" if blocked else "ran"

                if not hooked and not (case.no_wrapper or case.bridge_may_not_run):
                    # Nothing can be concluded. A command stopped by something
                    # other than the gate looks exactly like one the gate
                    # stopped, and reading that as a pass is how a project with
                    # no gate at all gets a clean bill of health.
                    inconclusive += 1
                    print(f"{UNKNOWN}{case.name}: the hook never ran, so the command {outcome}")
                    print("          proves nothing about the gate")
                    if note:
                        print(f"          {note}")
                    continue

                passed = case.either or blocked == case.must_block
                if passed:
                    mark = OK
                elif case.gap:
                    mark = GAP
                else:
                    mark = FAIL
                    problems += 1
                print(f"{mark}{case.name}: the command {outcome}")
                if not passed:
                    print(f"          {case.why}")
                if note:
                    print(f"          {note}")
        print()

    if inconclusive:
        # Louder than a failure, because it is worse than one. A failure says
        # the gate did the wrong thing; this says nothing reached the gate, and
        # every case that "blocked" did so for reasons nobody verified.
        print(
            f"{inconclusive} case(s) proved nothing: the hook never ran at all. "
            "Until that is fixed,\nthis project has no gate on it, however it looks."
        )
    if problems:
        print(f"{problems} case(s) failed. The gate is not doing what it claims.")
    if not problems and not inconclusive:
        print("Every case behaved. Gaps above are known and documented in the README.")
    return 1 if problems or inconclusive else 0
