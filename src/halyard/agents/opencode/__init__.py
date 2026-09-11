"""opencode, as a runtime Halyard can gate.

The fourth, and the first that is not driven by hooks. Three things about it
were measured rather than read, and each one would have produced a gate that
looked installed and did nothing:

**Its gate is a plugin, not a hooks file.** A module in `.opencode/plugins/`,
loaded at startup with no config entry and no package install — and loaded in
the TUI, not only under `opencode serve`, because the TUI embeds the same
server. That matters because the TUI is how anybody actually uses it.

**Its permission hook is declared and never called.** `@opencode-ai/plugin`
1.18.29 has a `permission.ask` whose signature reads exactly like a gate. It
was measured against real approvals, twice, and it never fired. What arrives is
a `permission.asked` event, and the answer goes back over the HTTP API. So the
bridge here hears a question and sends an answer rather than returning a
verdict — see `bridge/opencode.ts`, where that difference is written down.

**It does not ask by default.** Fifteen turns and a file edited produced no
permission event at all. Without a `permission` block in the project's own
config the plugin is wired and dead, which is why wiring writes both.

**Sessions are addressed by title, and a title can be set by a person.** This
paragraph said the opposite for a while — that there was nothing here to bind a
seat to — and it was wrong on the machine it was written on, which had a
session titled `alpha-engine-opencode-driver` sitting in the list the whole
time. `find_session` matches that title. A generated one does move as the
conversation moves, and that is a reason to name a session rather than a reason
to say naming is impossible.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from halyard.agents.base import SessionRef
from halyard.agents.opencode import wiring
from halyard.agents.spec import Hooks, RuntimeSpec

#: Older than anything, for sorting a session that records no time at all.
_EPOCH = datetime.fromtimestamp(0, tz=UTC)

#: Long enough for a cold start, short enough that `doctor` stays answerable.
TIMEOUT = 10.0


def _runner(settings=None):
    """Takes nothing from settings; the argument is the shared shape."""
    from halyard.agents.opencode.runner import OpencodeRunner

    return OpencodeRunner()


def _binary() -> str | None:
    return shutil.which("opencode")


def _version(binary: str) -> str | None:
    try:
        done = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip().splitlines()[-1].strip() if done.returncode == 0 else None


def _available_binary() -> list[tuple[str, str]]:
    """Whether this machine has the CLI at all."""
    found = _binary()
    if not found:
        return [
            ("fail", "no opencode CLI on PATH, so nothing here can be gated or reached"),
            ("", "install it, then run `halyard doctor` again"),
        ]
    lines = [("ok", f"opencode at {found}")]
    if version := _version(found):
        lines.append(("", f"version {version}"))
    return lines


def check_wired(hooks_file: Path, project_dir: Path, **_context) -> list[tuple[str, str]]:
    """Whether this project's gate is present *and* switched on.

    Two questions, and the second is the one worth asking. A plugin sitting in
    the right directory proves nothing on its own: the runtime asks about
    nothing unless its own configuration tells it to, so a project can be
    wired, loaded and silent. That was measured before any of this was written.
    """
    lines: list[tuple[str, str]] = []

    if not hooks_file.is_file():
        return [
            ("fail", f"no {wiring.PLUGINS / wiring.PLUGIN} — nothing is gating this project"),
            ("", f"halyard wire {project_dir}"),
        ]
    lines.append(("ok", f"{wiring.PLUGINS / wiring.PLUGIN} is in place"))

    config = project_dir / wiring.CONFIG
    try:
        document = wiring._read(config)
    except (OSError, ValueError):
        return [
            *lines,
            ("fail", f"{wiring.CONFIG} could not be read, so what it asks about is unknown"),
        ]

    permission = document.get("permission")
    permission = permission if isinstance(permission, dict) else {}
    silent = sorted(
        name for name, how in wiring.ASK.items() if not wiring.asks_about(permission.get(name), how)
    )
    if silent:
        lines.append(("fail", f"{wiring.CONFIG} does not ask about {', '.join(silent)}"))
        lines.append(("", "the plugin is loaded and will never be consulted about those"))
        lines.append(("", f"halyard wire {project_dir}"))
    else:
        lines.append(("ok", f"{wiring.CONFIG} asks about {', '.join(sorted(wiring.ASK))}"))

    # Named, not failed. These are answered inside the runtime, so they reach no
    # gate: nothing about them is written to the audit log and `/pause` does not
    # stop them. That is a thing to know about a project, not a thing wrong with
    # it — somebody chose each one, and the exceptions are what "always allow"
    # at the keyboard writes.
    for name in sorted(wiring.ASK):
        allowed = wiring.answered_without_asking(permission.get(name))
        if allowed:
            lines.append(("warn", f"{name} answers {', '.join(allowed)} without asking"))
            lines.append(("", "those reach no gate, so no audit record and `/pause` misses them"))
    return lines


def reachable(port: int | None) -> tuple[bool, str]:
    """Whether a server is answering on this port, and what it said.

    Nothing else in this package needs the network. This does, because the
    thing it is checking for cannot be seen any other way: the gate looks
    perfectly wired and delivers questions it can never answer.
    """
    if not port:
        return False, "no port configured"
    import urllib.error
    import urllib.request

    try:
        # A literal loopback URL, built here rather than taken from anywhere.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=3) as answered:
            return answered.status == 200, f"answered {answered.status}"
    except urllib.error.HTTPError as refused:
        # Answering at all is what is being tested. A 404 means something is
        # listening, which is the question.
        return True, f"answered {refused.code}"
    except Exception as unreachable:
        return False, str(unreachable)


def _known_models() -> dict[str, str] | None:
    """Every `provider/model` this opencode can run, with the name it shows for
    it — or None if the question could not be asked.

    Asked of the CLI rather than the server, because this has to answer when
    the TUI is closed too — `doctor` is often run to find out why it is. The
    verbose listing costs what the plain one does, 0.83s measured on 1.18.29,
    and carries the name.

    The name is half the point. The TUI shows `DeepSeek V4.1 Flash`; the id
    behind it is `deepseek/deepseek-flash`. The id a person writes from that
    name, `deepseek/deepseek-v4-flash`, exists too — and is the model *before*
    it. Suggesting ids by spelling alone recommended exactly that one, which is
    worse than saying nothing: it would have configured the older model on the
    strength of a check.

    **None and empty are different answers**, as they are for Codex's catalog.
    None means the question could not be asked, and nothing may be concluded
    from it. A listing that will not parse is None too, so a format change
    silences the check rather than turning it against a model that is fine.
    """
    found = _binary()
    if not found:
        return None
    try:
        done = subprocess.run(
            [found, "models", "--verbose"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return _parse_models(done.stdout or "") or None


def _parse_models(listing: str) -> dict[str, str]:
    """`provider/id` headers, each followed by that model's JSON, into id → name.

    Decoded as JSON rather than read line by line, because each block nests
    other `id` and `name` keys and only the top-level `name` is the one the TUI
    shows. A block that will not decode keeps its id with no name: a change in
    that format costs the names and never the ids.
    """
    decoder = json.JSONDecoder()
    headers = list(re.finditer(r"^([^\s{}]+/\S+)[ \t]*$", listing, re.MULTILINE))
    found: dict[str, str] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(listing)
        block = listing[header.end() : end].strip()
        try:
            parsed, _ = decoder.raw_decode(block)
        except ValueError:
            parsed = None
        shown = parsed.get("name") if isinstance(parsed, dict) else None
        found[header.group(1)] = shown if isinstance(shown, str) else ""
    return found


def _as_typed(name: str) -> str:
    """A display name the way somebody copying it into YAML would write it."""
    return re.sub(r"\s+", "-", name.strip()).casefold()


def _check_models(configured) -> list[tuple[str, str]]:
    """Whether the models the configuration names are ones opencode has.

    Measured the day this was written: `deepseek/deepseek-v4.1-flash` was added
    as the model to fall back to when a quota runs out, and this opencode knows
    four DeepSeek models and none of them has that id. The name belongs to
    `deepseek/deepseek-flash`, shown as DeepSeek V4.1 Flash; the id somebody
    would write from that name, `deepseek-v4-flash`, is the model before it.
    The configuration's own check passed, because it only asks whether
    `on_quota:` is in `models:`, and both were the same wrong name. The one
    button meant for the moment a provider stops answering would have offered a
    model that cannot answer either.

    Refreshing the list from models.dev did not add it, so that was not a stale
    cache. But a model announced this morning can be exactly that, and opencode
    may pass an id its list lacks straight to the provider — which is why this
    says the *list* has no such name rather than that the name cannot work, and
    says to refresh before it says to rename.

    A warning, never a failure. The runtime is still reachable and the session
    still found; a failure here would stop `doctor` checking the seat, and the
    channel would carry it to a phone as the reason a session could not be
    looked up — a wrong reason, in the place this project has worked hardest to
    give the right one.
    """
    if configured is None:
        return []
    wanted = [*configured.models, *([configured.on_quota] if configured.on_quota else [])]
    if not wanted:
        return []
    known = _known_models()
    if not known:
        return []
    # The name a person reads on the screen, keyed the way they would type it.
    shown_as: dict[str, str] = {}
    for model, shown in known.items():
        if shown:
            shown_as.setdefault(f"{model.split('/', 1)[0]}/{_as_typed(shown)}", model)
    said: list[tuple[str, str]] = []
    for name in dict.fromkeys(wanted):  # once each, in the order written
        if name in known:
            continue
        what = f"opencode's model list has no {name}"
        if name == configured.on_quota:
            what += " — the one offered when a quota runs out"
        said.append(("warn", what))
        meant = shown_as.get(name.casefold())
        if meant:
            # Exact, not a guess: this is the name the TUI shows for that id.
            said.append(("", f"that is how opencode shows {meant} ({known[meant]}) — use the id"))
            continue
        close = difflib.get_close_matches(name, sorted(known), n=2, cutoff=0.6)
        if close:
            said.append(("", f"did you mean {' or '.join(close)}?"))
        # The list lags new releases, and a model announced this morning is the
        # likeliest reason to add one — so the cheap step comes before renaming.
        said.append(("", "if it is newer than the list, `opencode models --refresh` first"))
    return said


def check_available(**_context) -> list[tuple[str, str]]:
    """Whether this machine can run it, and whether it can be reached.

    **The second half is not optional here.** This runtime's gate answers an
    approval by calling back into opencode's own HTTP API, and that API is
    served only when the TUI is started with `--port`. Its default is to serve
    nothing reachable — measured: `opencode` alone leaves a process listening
    on no TCP port at all, and `opencode --port 4096` answers immediately.

    A gate in that state is the worst shape this project knows: the question
    reaches the phone, the button does nothing, and every part of `wire` and
    every file on disk says the project is gated.
    """
    lines = list(_available_binary())
    if any(level == "fail" for level, _ in lines):
        return lines

    from halyard.core.config_file import runtime_settings

    try:
        configured = runtime_settings().get("opencode")
    except Exception:
        # A configuration that will not parse is somebody else's report to
        # make; this check is not the place to raise it a second time.
        configured = None
    port = configured.port if configured else None
    # Checked whether or not the server answers: the listing comes from the
    # CLI, and a closed TUI is exactly when somebody runs `doctor`.
    models_said = _check_models(configured)

    if not port:
        lines.append(("warn", "no `runtimes: opencode: port:` in halyard.yaml"))
        lines.append(("", "without one nothing here can tell whether the gate can answer"))
        lines.append(("", "add `port: 4096`, and start it with `opencode --port 4096`"))
        return lines + models_said

    answering, said = reachable(port)
    if answering:
        lines.append(("ok", f"opencode is answering on port {port}"))
        return lines + models_said

    # A failure, not a warning, and the reason is what happens next. Everything
    # after this asks opencode something — which session a seat means, whether
    # it is where it says it is — and with nobody answering, every one of those
    # comes back empty. Reported as a warning once, and the line underneath it
    # read "no session named alpha-engine-opencode-driver", which sent somebody
    # looking for a session that was there the whole time behind a server that
    # was not running.
    lines.append(("fail", f"nothing is answering on port {port} ({said})"))
    lines.append(("", "so nothing here can be asked which session a seat means"))
    lines.append(("", f"start it with `opencode --port {port}`, or leave a headless one"))
    lines.append(("", f"running with `opencode serve --port {port}` and attach to that"))
    lines.append(("", f"from a terminal with `opencode attach http://127.0.0.1:{port}`"))
    # After the failure and everything under it, so the channel — which
    # carries a failure and its continuation to a phone — stops before these.
    return lines + models_said


DEFAULT_PORT = 4096


def _port() -> int:
    """Where this machine's opencode answers.

    A default rather than a requirement: 4096 is what opencode's own
    documentation uses and what the one other tool doing this expects, so a
    configuration that says nothing still works.
    """
    from halyard.core.config_file import runtime_settings

    try:
        configured = runtime_settings().get("opencode")
    except Exception:
        configured = None
    return (configured.port if configured and configured.port else None) or DEFAULT_PORT


def _sessions(directory: str | None = None) -> list[dict] | None:
    """Every session this server knows, or None when it cannot be asked.

    The distinction is the point. An empty list means opencode answered and has
    nothing; None means nobody answered, which is a different sentence and was
    once given as the first — a seat was reported as naming a session that does
    not exist, when the truth was that opencode was not running.
    """
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    where = f"http://127.0.0.1:{_port()}/session"
    if directory:
        where += f"?directory={quote(directory, safe='')}"
    try:
        with urllib.request.urlopen(where, timeout=5) as answered:
            loaded = json.loads(answered.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return loaded if isinstance(loaded, list) else []


def _as_ref(session: dict, *, chosen: bool) -> SessionRef:
    def when(value) -> datetime | None:
        # Milliseconds since the epoch, which is what this runtime writes.
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            return None

    times = session.get("time") or {}
    return SessionRef(
        session_id=str(session.get("id") or ""),
        name=str(session.get("title") or ""),
        cwd=str(session.get("directory") or "") or None,
        named_by_a_person=chosen,
        started_at=when(times.get("created")),
        last_active=when(times.get("updated")),
    )


def find_session(name: str) -> SessionRef | None:
    """The session with this title, asked of the running opencode.

    Sessions here are addressed by their title. opencode writes one from the
    content of a conversation and rewrites it as that content moves, so a seat
    pointed at a generated title comes loose — but a title somebody set stays,
    and that is how this is used: one long-lived session, named to match the
    seat.

    Measured on a real project, where the difference is plain to read:

        alpha-engine-opencode-driver                    ← set by a person
        Kalan kapatma promptu — capstone-ekran          ← written by opencode
        Quarter lens comparison chart inflation prompt  ← written by opencode

    A match against a title the configuration asked for is taken as chosen by a
    person, because it is: somebody wrote that name in two places.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    listed = _sessions()
    if not listed:
        return None
    for session in listed:
        if str(session.get("title") or "").strip().casefold() == wanted:
            return _as_ref(session, chosen=True)
        if str(session.get("id") or "").strip() == (name or "").strip():
            # An id is unreadable and permanent, and somebody holding one
            # should not be told to go and find a title for it first.
            return _as_ref(session, chosen=False)
    return None


def list_sessions() -> list[SessionRef]:
    """What this machine's opencode has, newest first, for `halyard init`.

    Whether a title was chosen or generated is not recorded anywhere, so this
    does not claim to know. `find_session` can say, because a match against a
    configured name is evidence in itself; a bare listing has none.
    """
    listed = _sessions() or []
    refs = [_as_ref(session, chosen=False) for session in listed]
    return sorted(refs, key=lambda ref: ref.last_active or ref.started_at or _EPOCH, reverse=True)


RUNTIME = RuntimeSpec(
    name="opencode",
    human="opencode",
    binary="opencode",
    prefix="o",
    hooks=Hooks(
        # Not a hooks file. `settings` is what core reads when it needs to name
        # the file in the project that carries this runtime's gate, and for
        # this one that is the plugin. Nothing writes it through the hooks
        # path: `install` below is what puts it there.
        settings=str(wiring.PLUGINS / wiring.PLUGIN),
        matcher="|".join(sorted(wiring.ASK)),
        dialect="plugin",
    ),
    runner=_runner,
    find_session=find_session,
    list_sessions=list_sessions,
    sessions_hint="the session titles this machine's opencode has, `halyard sessions`",
    check_available=check_available,
    check_wired=check_wired,
    install=wiring.install,
    uninstall=wiring.uninstall,
    when_unanswered=(
        "nothing is denied and nothing is let through. This runtime has already\n"
        "  put the question on its own screen and is waiting there; a control plane\n"
        "  that cannot answer just leaves it for whoever is at the desk. So the\n"
        "  project keeps working with Halyard down — you answer it yourself."
    ),
)
