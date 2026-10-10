"""The questions a Codex Desktop chat asks and leaves waiting, and their answers.

Codex Desktop can ask the person something mid-turn — a choice, or a line of
text — through a tool, `request_user_input_async`, and carries on without
waiting. Nothing fires a hook for it, so the question sat on the desk's screen
and never reached a phone. What there is, is the rollout. Measured on a live
Desktop thread (CLI 0.160 to 0.162):

- the question is written twice, under one id: a `response_item` of type
  `function_call` named `request_user_input_async`, whose `arguments` hold
  `{"questions": [{"title", "options"?}]}`, and an `event_msg` `item_completed`
  whose `item` is an `AgentMessage` with `delivery: "async"`, its `id` the same
  `call_…` id and the same `questions`;
- a question with no `options` takes free text;
- the answer is a user message whose whole text is a JSON list between
  `<send_user_message_question_reply>` tags, each entry naming the question by
  `["request_user_input_async", <call id>, <index>]`.

So a question is read from the lines appended to the rollout, and answered by
queueing that envelope into the thread — which is how the Desktop app sends it
too. These are the app's own internals rather than a published protocol, and
are worth measuring again when Codex changes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from halyard.agents.spec import AskedQuestion

TOOL = "request_user_input_async"
OPEN = "<send_user_message_question_reply>"
CLOSE = "</send_user_message_question_reply>"

#: Only lines carrying one of these are parsed. A rollout is mostly tool output,
#: and decoding every line of it to find a question would be most of the cost.
_MARKERS = (TOOL, '"questions"', OPEN)


def question_id(call_id: str, index: int) -> str:
    """How the Desktop app names one question of one call, in its answers."""
    return json.dumps([TOOL, call_id, index], separators=(",", ":"))


def read(lines: Iterable[str]) -> tuple[list[AskedQuestion], dict[str, str]]:
    """The questions asked in these lines, and the answers given in them, by id.

    One question once, however many of its two records the lines hold.
    """
    asked: dict[str, AskedQuestion] = {}
    answered: dict[str, str] = {}
    for line in lines:
        if not any(marker in line for marker in _MARKERS):
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict):
            _consume(record, asked, answered)
    return list(asked.values()), answered


def envelope(question: str, title: str, answer: str) -> str:
    """The message that answers one question, as the Desktop app writes it."""
    reply = [{"questionItemId": question, "question": title, "answer": answer}]
    return f"{OPEN}\n{json.dumps(reply, ensure_ascii=False)}\n{CLOSE}"


def _consume(record: dict, asked: dict[str, AskedQuestion], answered: dict[str, str]) -> None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    if record.get("type") == "response_item":
        if payload.get("type") == "function_call" and payload.get("name") == TOOL:
            try:
                arguments = json.loads(payload.get("arguments") or "")
            except (ValueError, TypeError):
                return
            if isinstance(arguments, dict):
                _add(asked, payload.get("call_id"), arguments.get("questions"))
        elif payload.get("type") == "message" and payload.get("role") == "user":
            _answers(answered, payload.get("content"))
    elif record.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item")
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind in ("AgentMessage", "agentMessage") and item.get("delivery") == "async":
            _add(asked, item.get("id"), item.get("questions"))
        elif kind in ("UserMessage", "userMessage"):
            _answers(answered, item.get("content"))
        elif (
            kind in ("SteeringUserMessage", "steeringUserMessage")
            and item.get("status") == "accepted"
        ):
            _answers(answered, item.get("input"))


def _add(asked: dict[str, AskedQuestion], call_id: object, questions: object) -> None:
    if not isinstance(call_id, str) or not call_id or not isinstance(questions, list):
        return
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        title = question.get("title")
        options = question.get("options") or []
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            continue
        ident = question_id(call_id, index)
        asked.setdefault(ident, AskedQuestion(id=ident, title=title, options=tuple(options)))


def _answers(answered: dict[str, str], content: object) -> None:
    """The answers in one user message, when its whole text is the envelope."""
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return
    text = content[0].get("text")
    if not isinstance(text, str):
        return
    text = text.strip()
    if not (text.startswith(OPEN) and text.endswith(CLOSE)):
        return
    try:
        replies = json.loads(text[len(OPEN) : -len(CLOSE)])
    except ValueError:
        return
    for reply in replies if isinstance(replies, list) else [replies]:
        if not isinstance(reply, dict):
            continue
        ident, answer = reply.get("questionItemId"), reply.get("answer")
        if isinstance(ident, str) and isinstance(answer, str):
            answered[ident] = answer
