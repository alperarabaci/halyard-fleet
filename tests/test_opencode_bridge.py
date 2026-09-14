"""The opencode bridge, run as the TypeScript it is.

Nothing tested this file before. It is the one piece of Halyard that runs
inside somebody else's process, in another language, and until now it was
checked by reading it. Node 22 can strip TypeScript's types and load it, and the
bridge uses only what Node also has — `fetch` and `node:fs` — so its behaviour
can be driven from here: a `permission.asked` event in, the body it posts out.

Skipped where there is no Node new enough, which may include CI. A test that
cannot run is not a test that passed, and a skip says so where a pass would not.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "opencode.ts"

#: Loads the bridge the way opencode does, with `fetch` standing in for the
#: control plane, and prints every approval body it would have posted.
DRIVER = r"""
import { pathToFileURL } from "node:url"
const posted = []
globalThis.fetch = async (url, init) => {
  posted.push({ url: String(url), body: JSON.parse(init.body) })
  return { ok: true, json: async () => ({ decision: "deny" }) }
}
const events = JSON.parse(process.argv[3])
const { HalyardGate } = await import(pathToFileURL(process.argv[2]).href)
const gate = await HalyardGate({
  client: { postSessionIdPermissionsPermissionId: async () => ({}) },
  directory: "/repo",
  worktree: "/repo",
})
for (const properties of events) {
  await gate.event({ event: { type: "permission.asked", properties } })
}
await new Promise((done) => setTimeout(done, 100))
const approvals = posted.filter((p) => p.url.endsWith("/v1/approvals")).map((p) => p.body)
console.log(JSON.stringify(approvals))
"""


def _node() -> str | None:
    """A Node that can load TypeScript, or None. Type stripping arrived in 22.6."""
    found = shutil.which("node")
    if not found:
        return None
    version = subprocess.run([found, "--version"], capture_output=True, text=True).stdout
    numbers = [int(part) for part in re.findall(r"\d+", version)[:2]]
    return found if len(numbers) == 2 and tuple(numbers) >= (22, 6) else None


def posted(tmp_path: Path, *events: dict, home: str = "/Users/somebody") -> list[dict]:
    """What the bridge would send the control plane for these events."""
    node = _node()
    if node is None:
        pytest.skip("needs Node 22.6 or newer, to load the bridge's TypeScript")
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    done = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            str(driver),
            str(BRIDGE),
            json.dumps(list(events)),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": home},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def asked(permission: str, **rest) -> dict:
    """A `permission.asked` event, in the shape 1.18.29's own source builds."""
    return {"id": "per_1", "sessionID": "ses_1", "permission": permission, **rest}


def test_a_directory_question_is_sent_in_opencode_s_own_words(tmp_path: Path) -> None:
    """The case on the phone: a command writing into /tmp, and opencode asking
    about /tmp rather than about the command."""
    [body] = posted(
        tmp_path,
        asked(
            "external_directory",
            patterns=["/tmp/*"],
            always=["/tmp/*"],
            metadata={"command": "make test-guards 2>&1 | tail -2 > /tmp/g1.txt"},
        ),
    )

    assert body["asks"] == "Access external directory /tmp"
    assert body["patterns"] == ["/tmp/*"]
    assert body["tool"] == "external_directory"
    assert body["command"].startswith("make test-guards")


def test_the_directory_comes_from_the_metadata_first_with_home_shortened(tmp_path: Path) -> None:
    """The order the TUI uses: `parentDir`, then `filepath`, then the pattern."""
    [body] = posted(
        tmp_path,
        asked(
            "external_directory",
            patterns=["/Users/somebody/.config/*"],
            metadata={"parentDir": "/Users/somebody/.config/tool", "command": "ls"},
        ),
        home="/Users/somebody",
    )

    assert body["asks"] == "Access external directory ~/.config/tool"


def test_a_shell_call_sends_nothing_more(tmp_path: Path) -> None:
    """For a shell call the pattern *is* the command — measured, both present
    and equal — and a card showing it twice would be noise."""
    [body] = posted(
        tmp_path, asked("bash", patterns=["git status"], metadata={"command": "git status"})
    )

    assert "asks" not in body
    assert "patterns" not in body


#: The bridge with a control plane whose approvals either never come back —
#: the card is still out — or come back at once with the given decision, fed a
#: run of events of any type. Prints every body posted and every answer given
#: to opencode.
SEQUENCE = r"""
import { pathToFileURL } from "node:url"
const posted = []
const answered = []
globalThis.fetch = async (url, init) => {
  posted.push({ url: String(url), body: JSON.parse(init.body) })
  if (String(url).endsWith("/v1/approvals")) {
    if (process.argv[4] === "hang") return new Promise(() => {})
    return { ok: true, json: async () => ({ decision: process.argv[4] }) }
  }
  return { ok: true, json: async () => ({ closed: true }) }
}
const events = JSON.parse(process.argv[3])
const { HalyardGate } = await import(pathToFileURL(process.argv[2]).href)
const gate = await HalyardGate({
  client: { postSessionIdPermissionsPermissionId: async (call) => { answered.push(call) } },
  directory: "/repo",
  worktree: "/repo",
})
for (const event of events) {
  await gate.event({ event })
  await new Promise((done) => setTimeout(done, 20))
}
await new Promise((done) => setTimeout(done, 100))
console.log(JSON.stringify({ posted, answered }))
process.exit(0)
"""


def played(tmp_path: Path, *events: dict, approvals: str = "hang") -> dict:
    """What the bridge posts, and what it answers opencode, for a run of events."""
    node = _node()
    if node is None:
        pytest.skip("needs Node 22.6 or newer, to load the bridge's TypeScript")
    driver = tmp_path / "sequence.mjs"
    driver.write_text(SEQUENCE, encoding="utf-8")
    done = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            str(driver),
            str(BRIDGE),
            json.dumps(list(events)),
            approvals,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": "/Users/somebody"},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def replied(reply: str, **spelled) -> dict:
    """A `permission.replied` event, in 1.18.30's shape unless told otherwise."""
    return {
        "type": "permission.replied",
        "properties": spelled or {"sessionID": "ses_1", "requestID": "per_1", "reply": reply},
    }


def closings(result: dict) -> list[dict]:
    return [p["body"] for p in result["posted"] if p["url"].endswith("/v1/approvals/answered")]


def test_a_question_goes_under_its_own_id(tmp_path: Path) -> None:
    """The id the reply event will name it by. One tool call can raise two
    questions, and they must not share a card."""
    [body] = posted(tmp_path, asked("bash", metadata={"command": "ls"}, tool={"callID": "call_1"}))

    assert body["tool_use_id"] == "per_1"


def test_an_answer_at_the_desk_closes_the_card(tmp_path: Path) -> None:
    """The desk won the race: the card is told, with what the desk said."""
    result = played(
        tmp_path,
        {"type": "permission.asked", "properties": asked("bash", metadata={"command": "ls"})},
        replied("once"),
    )

    assert closings(result) == [
        {"session_id": "ses_1", "agent_id": "opencode", "tool_use_id": "per_1", "decision": "allow"}
    ]
    assert result["answered"] == []


def test_a_refusal_at_the_desk_closes_the_card_as_denied(tmp_path: Path) -> None:
    result = played(
        tmp_path,
        {"type": "permission.asked", "properties": asked("bash", metadata={"command": "rm -r x"})},
        replied("reject"),
    )

    [closing] = closings(result)
    assert closing["decision"] == "deny"


def test_an_older_opencode_spells_the_answer_differently(tmp_path: Path) -> None:
    result = played(
        tmp_path,
        {"type": "permission.asked", "properties": asked("bash", metadata={"command": "ls"})},
        replied("", sessionID="ses_1", permissionID="per_1", response="always"),
    )

    [closing] = closings(result)
    assert closing["tool_use_id"] == "per_1"
    assert closing["decision"] == "allow"


def test_the_bridges_own_answer_is_not_mistaken_for_the_desks(tmp_path: Path) -> None:
    """Relaying the phone's answer makes opencode say it was answered, and that
    echo must not come back as an answer from the desk."""
    result = played(
        tmp_path,
        {"type": "permission.asked", "properties": asked("bash", metadata={"command": "ls"})},
        replied("reject"),
        approvals="deny",
    )

    assert [call["path"]["permissionID"] for call in result["answered"]] == ["per_1"]
    assert closings(result) == []
