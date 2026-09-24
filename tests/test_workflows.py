"""Tests for `halyard.workflows` — what the last line of a reply decided, where
the run goes next, what each step is told about it, and what the run remembers
across a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from halyard.core.config_file import Decisions, Step
from halyard.workflows import (
    Decision,
    Next,
    Round,
    Run,
    after,
    clear,
    current,
    decided,
    lines_for,
    read,
    record,
    save,
)

AT = datetime(2026, 9, 16, 18, 21, tzinfo=UTC)

FLOW = ("to_nav", "review", "discover", "discovered")
STEPS = {
    "to_nav": Step(name="to_nav", transition="to_nav", seat="nav"),
    "review": Step(name="review", transition="review", seat="xreview", rounds=2),
    "discover": Step(name="discover", transition="driver_discover", seat="xdrv", rounds=2),
    "discovered": Step(name="discovered", transition="discover_completed", seat="nav", rounds=2),
}

#: The review of level 3: the reviewer's decision is the navigator's to act on.
REVIEWED = ("review", "reviewed", "discover", "discovered")
REVIEWED_STEPS = {
    "review": Step(name="review", transition="review", seat="xreview", rounds=2),
    "reviewed": Step(
        name="reviewed", transition="to_nav", seat="nav", rounds=2, decided_by="review"
    ),
    "discover": Step(name="discover", transition="driver_discover", seat="xdrv", rounds=2),
    "discovered": Step(name="discovered", transition="discover_completed", seat="nav", rounds=2),
}

#: A project whose prompts ask for words of their own, and one left as it was.
GO_HOLD = Decisions(forward="go", wait="hold")
SEATS = {"review": "xreview", "reviewed": "nav", "discover": "xdrv", "discovered": "nav"}


def a_run(step: int = 1, **more) -> Run:
    return Run(
        workflow="level3",
        step=step,
        since=more.pop("since", AT),
        chat="-100777",
        waiting_for="xreview",
        **more,
    )


def next_after(decision: Decision | None, step: int = 1, **taken):
    return after(decision, run=a_run(step), flow=FLOW, steps=STEPS, taken=taken)


def reviewed_after(decision: Decision | None, step: int, *, carried: str = "", **taken):
    return after(
        decision,
        run=a_run(step, carried=carried),
        flow=REVIEWED,
        steps=REVIEWED_STEPS,
        taken=taken,
    )


# --- what the last line said ---------------------------------------------------


def test_the_decision_is_read_from_the_last_line() -> None:
    reply = "1 premise — nothing found\nBLOCKER: none\nDECISION: forward"

    assert read(reply) is Decision.FORWARD


def test_a_line_that_is_only_the_word_decides_too() -> None:
    assert read("It needs the loader fixed first.\nback") is Decision.BACK


def test_the_same_word_in_a_paragraph_decides_nothing() -> None:
    """A flow that moved on prose would move on a sentence about what somebody
    was thinking of doing."""
    reply = "We could go forward from here, but the count is wrong.\nStill measuring."

    assert read(reply) is None


def test_case_spacing_and_emphasis_do_not_matter() -> None:
    """A model that wants its answer seen makes it bold; it is the same answer."""
    assert read("done\n  decision:   Forward  ") is Decision.FORWARD
    assert read("**DECISION: back**") is Decision.BACK
    assert read("`DECISION: wait`") is Decision.WAIT
    assert read("- DECISION: forward.") is Decision.FORWARD


def test_the_label_is_the_project_s_own() -> None:
    """Whatever comes before the colon is not read: a project names its line."""
    assert read("RESULT: back") is Decision.BACK
    assert read("DECISION: back") is Decision.BACK


def test_a_reply_that_decides_nothing_is_nothing() -> None:
    assert read("All 42 tests pass.") is None
    assert read("DECISION: sideways") is None
    assert read("") is None
    assert read(None) is None


def test_a_project_s_own_words_are_read_instead_of_the_defaults() -> None:
    assert read("RESULT: go", GO_HOLD) is Decision.FORWARD
    assert read("hold", GO_HOLD) is Decision.WAIT
    assert read("RESULT: forward", GO_HOLD) is None, "a word renamed is no longer said"


def test_a_word_a_project_left_out_keeps_its_own_name() -> None:
    assert read("RESULT: back", GO_HOLD) is Decision.BACK


# --- where the run goes next ---------------------------------------------------


def test_forward_takes_the_next_step() -> None:
    assert next_after(Decision.FORWARD) == Next(step=2, decided=Decision.FORWARD)


def test_a_reply_that_decided_nothing_carries_on() -> None:
    moving = next_after(None)

    assert moving.step == 2
    assert moving.stop == ""


def test_back_is_one_step() -> None:
    moving = next_after(Decision.BACK)

    assert (moving.step, moving.back) == (0, True)


def test_back_from_the_first_step_stops() -> None:
    moving = next_after(Decision.BACK, step=0)

    assert moving.step is None
    assert "nothing before it" in moving.stop


def test_wait_stops_the_run() -> None:
    moving = next_after(Decision.WAIT)

    assert moving.step is None
    assert moving.stop


def test_the_last_step_ends_the_flow() -> None:
    moving = next_after(Decision.FORWARD, step=len(FLOW) - 1)

    assert moving.done is True
    assert moving.step is None


def test_a_step_past_its_rounds_is_offered_rather_than_taken(monkeypatch) -> None:
    """The lesson of alpha-engine#361: the round after the last one is a
    person's to start. The step is named so it can be sent anyway."""
    moving = next_after(Decision.FORWARD, step=0, review=2)

    assert moving.step == 1
    assert "round 3 of 2" in moving.stop


def test_a_step_goes_once_unless_it_says_otherwise() -> None:
    """Going round is something a project writes down: `to_nav` says nothing,
    so sending the work back to it waits for the operator."""
    moving = next_after(Decision.BACK, step=1, to_nav=1, review=1)

    assert moving.step == 0
    assert "round 2 of 1" in moving.stop


def test_rounds_are_counted_by_the_step_not_by_its_transition() -> None:
    """Two steps sharing a transition do not share a count, and the transition's own
    presses count nothing: the rounds that stop a loop are the loop's own."""
    counted = next_after(Decision.BACK, step=3, discover=2)
    handed = next_after(Decision.BACK, step=3, driver_discover=2)

    assert (counted.step, handed.step) == (2, 2)
    assert "round 3 of 2" in counted.stop
    assert handed.stop == ""


def test_a_run_from_an_older_configuration_stops_rather_than_guessing() -> None:
    moving = after(
        Decision.FORWARD,
        run=a_run(0),
        flow=("to_nav", "gone"),
        steps=STEPS,
        taken={},
    )

    assert moving.step is None
    assert "gone" in moving.stop


# --- a step that acts on the decision before it --------------------------------


def test_a_review_goes_to_the_step_that_acts_on_it_whichever_way_it_decided() -> None:
    """The navigator holds the context, so a reviewer's back reaches it first."""
    for decision in (Decision.BACK, Decision.FORWARD):
        moving = reviewed_after(decision, 0, review=1)

        assert (moving.step, moving.back, moving.carried) == (1, False, decision)


def test_a_review_that_says_wait_stops_at_once() -> None:
    moving = reviewed_after(Decision.WAIT, 0, review=1)

    assert moving.step is None
    assert moving.carried is None
    assert moving.stop


def test_a_review_that_decided_nothing_carries_nothing() -> None:
    moving = reviewed_after(None, 0, review=1)

    assert (moving.step, moving.carried) == (1, None)


def test_the_navigator_s_reply_goes_where_the_review_said() -> None:
    back = reviewed_after(None, 1, carried="back", review=1, reviewed=1)
    forward = reviewed_after(None, 1, carried="forward", review=1, reviewed=1)

    assert (back.step, back.back, back.decided) == (0, True, Decision.BACK)
    assert (forward.step, forward.back) == (2, False)


def test_the_navigator_s_own_decision_wins() -> None:
    moving = reviewed_after(Decision.FORWARD, 1, carried="back", review=1, reviewed=1)

    assert (moving.step, moving.decided) == (2, Decision.FORWARD)


def test_a_review_sent_back_again_still_counts_its_rounds() -> None:
    moving = reviewed_after(None, 1, carried="back", review=2, reviewed=2)

    assert moving.step == 0
    assert "round 3 of 2" in moving.stop


def test_only_the_step_that_names_it_acts_on_a_carried_decision() -> None:
    """A decision left over on a run moves nothing at a step that decides for itself."""
    moving = reviewed_after(None, 2, carried="back", discover=1)

    assert (moving.step, moving.back) == (3, False)


# --- phases: the steps that go round again going forward ---------------------

#: A plan in parts: the navigator once, then discover, develop and verify once
#: per part, then close.
PHASED = ("to_nav", "discover", "develop", "verified", "close")
PHASED_STEPS = {
    "to_nav": Step(name="to_nav", transition="to_nav", seat="nav"),
    "discover": Step(name="discover", transition="driver_discover", seat="xdrv", rounds=2),
    "develop": Step(name="develop", transition="driver_develop", seat="xdrv"),
    "verified": Step(name="verified", transition="to_nav", seat="nav", rounds=2),
    "close": Step(name="close", transition="close", seat="nav"),
}
STRETCH = (1, 3)


def phased_after(
    decision: Decision | None,
    step: int,
    *,
    phase: int = 1,
    entered: int = -1,
    named: str = "",
    most: int = 3,
    taken: dict[str, int] | None = None,
) -> Next:
    return after(
        decision,
        run=a_run(step, phase=phase, entered=entered),
        flow=PHASED,
        steps=PHASED_STEPS,
        taken=taken or {},
        stretch=STRETCH,
        most=most,
        named=named,
    )


def test_next_can_name_the_step_the_next_phase_starts_at() -> None:
    assert decided("Phase 1 is in.\nDECISION: next develop") == (Decision.NEXT, "develop")
    assert decided("DECISION: **next `develop`**") == (Decision.NEXT, "develop")
    assert decided("DECISION: next") == (Decision.NEXT, "")


def test_a_project_s_own_word_for_next_names_a_step_too() -> None:
    words = Decisions(next="sonraki")

    assert decided("KARAR: sonraki develop", words) == (Decision.NEXT, "develop")
    assert decided("KARAR: next develop", words) == (None, "")


def test_more_than_a_step_after_next_decides_nothing() -> None:
    """A sentence is not a decision, and a flow that moved on one would move on
    a model thinking aloud."""
    assert decided("DECISION: next develop after lunch") == (None, "")
    assert decided("DECISION: forward please") == (None, "")


def test_next_at_the_end_of_a_phase_starts_the_next_from_its_first_step() -> None:
    moving = phased_after(Decision.NEXT, 3)

    assert (moving.step, moving.phase, moving.entered, moving.stop) == (1, 2, 1, "")
    assert moving.skipped == ()


def test_next_can_start_the_next_phase_further_on() -> None:
    """A phase that is only a proposal has nothing to discover."""
    moving = phased_after(Decision.NEXT, 3, named="develop")

    assert (moving.step, moving.phase, moving.entered) == (2, 2, 2)
    assert moving.skipped == ("discover",)


def test_next_naming_a_step_outside_the_phases_stops() -> None:
    moving = phased_after(Decision.NEXT, 3, named="close")

    assert moving.step is None
    assert "not one of this workflow's phase steps" in moving.stop


def test_next_anywhere_but_the_end_of_a_phase_stops() -> None:
    """A seat that lost its place; the run does not guess which phase it meant."""
    moving = phased_after(Decision.NEXT, 2)

    assert moving.step is None
    assert "not where a phase ends" in moving.stop


def test_next_in_a_workflow_without_phases_stops() -> None:
    moving = next_after(Decision.NEXT, step=1)

    assert moving.step is None
    assert "no phases" in moving.stop


def test_forward_at_the_end_of_a_phase_leaves_the_phases() -> None:
    moving = phased_after(Decision.FORWARD, 3, phase=2)

    assert (moving.step, moving.phase, moving.stop) == (4, None, "")


def test_a_phase_that_ends_undecided_stops_with_the_next_one_ready() -> None:
    """Carrying on would leave the phases with nobody having said the work was
    done. The next phase is made ready, and so is the way out of them."""
    moving = phased_after(None, 3)

    assert moving.stop == "phase 1 ended with no decision"
    assert (moving.step, moving.phase, moving.leaving) == (1, 2, 4)


def test_a_wait_at_the_end_of_a_phase_offers_the_next_one_or_the_way_out() -> None:
    """Between parts the operator applies what was made; what follows is the
    next part or the end of them, not whichever step comes next in the list."""
    moving = phased_after(Decision.WAIT, 3)

    assert moving.stop == "it was asked to wait"
    assert (moving.step, moving.phase, moving.leaving, moving.decided) == (
        1,
        2,
        4,
        Decision.WAIT,
    )


def test_a_wait_inside_a_phase_stops_as_any_wait_does() -> None:
    moving = phased_after(Decision.WAIT, 2)

    assert (moving.step, moving.leaving, moving.stop) == (None, None, "it was asked to wait")


def test_the_phases_stop_past_their_count_with_that_phase_ready() -> None:
    moving = phased_after(Decision.NEXT, 3, phase=3)

    assert "phase 4 would be past the 3 allowed" in moving.stop
    assert (moving.step, moving.phase, moving.leaving) == (1, 4, 4)


def test_rounds_start_again_in_each_phase() -> None:
    """Phase 2's discover is not phase 1's going round again."""
    fresh = phased_after(Decision.BACK, 2, phase=2, entered=1, taken={"discover@1": 2})
    spent = phased_after(Decision.BACK, 2, phase=2, entered=1, taken={"discover@2": 2})

    assert (fresh.step, fresh.stop) == (1, "")
    assert "round 3 of 2" in spent.stop


def test_steps_outside_the_phases_count_once_for_the_run() -> None:
    moving = phased_after(Decision.FORWARD, 3, phase=2, taken={"close": 1})

    assert moving.step == 4
    assert "round 2 of 1" in moving.stop


def test_back_from_where_a_phase_started_goes_to_the_end_of_the_one_before() -> None:
    """That is where what it was handed came from."""
    moving = phased_after(Decision.BACK, 1, phase=2, entered=1)

    assert (moving.step, moving.back, moving.phase) == (3, True, 1)


def test_back_in_the_first_phase_is_the_step_before_as_ever() -> None:
    moving = phased_after(Decision.BACK, 1, phase=1, entered=1)

    assert (moving.step, moving.phase) == (0, None)


def test_entering_the_phases_marks_where_the_first_one_started() -> None:
    moving = phased_after(Decision.FORWARD, 0)

    assert (moving.step, moving.entered) == (1, 1)


def phased_told(step: int, *, phase: int = 1, **taken) -> list[str]:
    return lines_for(
        a_run(step, phase=phase),
        flow=PHASED,
        steps=PHASED_STEPS,
        taken=taken,
        seats={
            "to_nav": "nav",
            "discover": "xdrv",
            "develop": "xdrv",
            "verified": "nav",
            "close": "nav",
        },
        stretch=STRETCH,
    )


def test_a_step_of_the_phases_is_told_its_phase() -> None:
    lines = phased_told(2, phase=2)

    assert lines[:2] == ["Workflow: level3 · step 3 of 5 · develop", "Phase: 2"]
    assert not any("next" in line for line in lines), "next means nothing mid-phase"


def test_the_end_of_a_phase_is_told_where_next_and_forward_go() -> None:
    lines = phased_told(3, phase=2)

    assert "forward (→ close, nav)" in lines[2]
    assert "wait (→ the operator)" in lines[2]
    assert lines[3] == (
        "This step ends phase 2: next (→ discover, xdrv, phase 3) starts the next one, "
        "and next <step> starts it at another of discover, develop, verified; forward "
        "leaves the phases. wait, or a reply with no decision, stops for the operator, "
        "who starts the next phase or leaves them."
    )


def test_a_step_outside_the_phases_is_told_no_phase() -> None:
    assert not any(line.startswith("Phase:") for line in phased_told(0))


# --- what each step is told ----------------------------------------------------


def told(
    step: int,
    *,
    carried: str = "",
    sent_back_by: str = "",
    words: Decisions | None = None,
    **taken,
) -> list[str]:
    return lines_for(
        a_run(step, carried=carried),
        flow=REVIEWED,
        steps=REVIEWED_STEPS,
        taken=taken,
        seats=SEATS,
        words=words,
        sent_back_by=sent_back_by,
    )


def test_a_step_says_where_each_word_on_its_last_line_takes_the_work() -> None:
    assert told(3, discover=1, discovered=1) == [
        "Workflow: level3 · step 4 of 4 · discovered",
        "Decide on your last line: forward (→ the workflow ends) · "
        "back (→ discover, xdrv, round 2 of 2) · wait (→ the operator)",
    ]


def test_a_step_is_told_the_project_s_own_words() -> None:
    assert told(3, words=GO_HOLD, discover=1, discovered=1)[1] == (
        "Decide on your last line: go (→ the workflow ends) · "
        "back (→ discover, xdrv, round 2 of 2) · hold (→ the operator)"
    )


def test_a_review_is_told_its_decision_goes_to_the_step_that_acts_on_it() -> None:
    assert told(0, review=1)[1] == (
        "Decide on your last line: forward or back "
        "(→ reviewed, nav, who acts on it) · wait (→ the operator)"
    )


def test_the_navigator_is_told_what_the_review_decided_and_how_to_overrule_it() -> None:
    assert told(1, carried="back", review=1, reviewed=1) == [
        "Workflow: level3 · step 2 of 4 · reviewed",
        "Already decided by review (xreview): back (→ review, xreview, round 2 of 2)",
        "To overrule it, decide on your last line: forward (→ discover, xdrv) "
        "or wait (→ the operator)",
    ]


def test_a_navigator_with_nothing_carried_decides_for_itself() -> None:
    assert told(1, review=1, reviewed=1)[1] == (
        "Decide on your last line: forward (→ discover, xdrv) · "
        "back (→ review, xreview, round 2 of 2) · wait (→ the operator)"
    )


def test_a_step_past_its_rounds_is_said_to_wait_for_the_operator() -> None:
    assert told(1, carried="back", review=2, reviewed=2)[1] == (
        "Already decided by review (xreview): back "
        "(→ review, xreview, round 3 of 2, which waits for the operator)"
    )


def test_a_step_the_work_came_back_to_says_who_sent_it() -> None:
    lines = told(0, sent_back_by="nav (navigator) at 18:21", review=2)

    assert lines[1] == "Sent back by: nav (navigator) at 18:21"


# --- what a restart must not lose ----------------------------------------------


def test_a_run_survives_being_written_and_read(tmp_path: Path) -> None:
    kept = tmp_path / "projects" / "alpha-engine" / "workflow-runs.json"

    save(kept, "alpha-engine#361", a_run(2))

    assert current(kept, "alpha-engine#361") == a_run(2)


def test_a_carried_decision_survives_a_restart(tmp_path: Path) -> None:
    kept = tmp_path / "workflow-runs.json"

    save(kept, "alpha-engine#361", a_run(1, carried="back").waiting_on("nav"))

    assert current(kept, "alpha-engine#361").carried == "back"


def test_moving_on_leaves_a_carried_decision_behind() -> None:
    assert a_run(1, carried="back").at(2).carried == ""
    assert a_run(0).at(1, carried="forward").carried == "forward"


def test_a_stopped_run_says_why_and_keeps_the_seat_it_heard_from(tmp_path: Path) -> None:
    """What it carries on with, when somebody sends the step it stopped
    before, is that seat's reply."""
    kept = tmp_path / "workflow-runs.json"

    save(kept, "alpha-engine#361", a_run(1).held("it was asked to wait"))

    held = current(kept, "alpha-engine#361")
    assert held is not None
    assert (held.stopped, held.waiting) == ("it was asked to wait", False)
    assert held.waiting_for == "xreview"


def test_each_piece_of_work_has_its_own_run(tmp_path: Path) -> None:
    kept = tmp_path / "workflow-runs.json"

    save(kept, "alpha-engine#361", a_run(1))
    save(kept, "alpha-engine#362", a_run(3))

    assert current(kept, "alpha-engine#361").step == 1
    assert current(kept, "alpha-engine#362").step == 3


def test_a_run_that_is_cleared_is_gone(tmp_path: Path) -> None:
    kept = tmp_path / "workflow-runs.json"
    save(kept, "alpha-engine#361", a_run(1))

    clear(kept, "alpha-engine#361")

    assert current(kept, "alpha-engine#361") is None


def test_nothing_written_is_no_run(tmp_path: Path) -> None:
    assert current(tmp_path / "workflow-runs.json", "alpha-engine#361") is None


def test_a_file_that_cannot_be_read_is_no_run(tmp_path: Path) -> None:
    kept = tmp_path / "workflow-runs.json"
    kept.write_text("{not json")

    assert current(kept, "alpha-engine#361") is None
    save(kept, "alpha-engine#361", a_run(1))
    assert current(kept, "alpha-engine#361") is not None


def test_a_run_keeps_its_phase_and_its_rounds_across_a_restart(tmp_path: Path) -> None:
    kept = tmp_path / "workflow-runs.json"
    run = a_run(2, phase=2, entered=1, rounds={"discover@2": (Round(at=AT, to="xdrv"),)})

    save(kept, "alpha-engine#386", run)

    assert current(kept, "alpha-engine#386") == run


def test_where_a_phase_end_would_leave_to_is_kept_only_until_the_run_moves(tmp_path: Path) -> None:
    """So the button on an old card cannot move a run that has gone on since."""
    kept = tmp_path / "workflow-runs.json"
    stopped = a_run(1, phase=2, entered=1).held("phase 1 ended with no decision", leaving=4)

    save(kept, "alpha-engine#386", stopped)

    assert current(kept, "alpha-engine#386").leaving == 4
    assert stopped.at(2).leaving == -1
    assert stopped.waiting_on("xdrv").leaving == -1
    assert stopped.held("it was asked to wait").leaving == -1


def test_a_round_is_counted_into_the_run_as_it_is_now(tmp_path: Path) -> None:
    """The seat can take the message after the run was saved again, so the
    round goes into what is on disk, not into the run it was sent from."""
    kept = tmp_path / "workflow-runs.json"
    save(kept, "alpha-engine#386", a_run(1).waiting_on("xdrv"))

    assert record(kept, "alpha-engine#386", "discover@1", to="xdrv", now=AT) == 1
    assert record(kept, "alpha-engine#386", "discover@1", to="xdrv", now=AT) == 2

    found = current(kept, "alpha-engine#386")
    assert found is not None and found.waiting_for == "xdrv"
    assert found.taken() == {"discover@1": 2}


def test_nothing_is_counted_for_a_run_that_is_gone(tmp_path: Path) -> None:
    """Somebody stopped it while its step was on its way."""
    kept = tmp_path / "workflow-runs.json"

    assert record(kept, "alpha-engine#386", "discover@1", to="xdrv") == 0
    assert current(kept, "alpha-engine#386") is None


def test_a_run_written_before_phases_reads_as_the_first_phase(tmp_path: Path) -> None:
    kept = tmp_path / "workflow-runs.json"
    kept.write_text(
        '{"alpha-engine#361": {"workflow": "level3", "step": 2, '
        '"since": "2026-09-16T18:21:00+00:00", "chat": "-100777", "waiting": true}}'
    )

    found = current(kept, "alpha-engine#361")

    assert found is not None
    assert (found.phase, found.entered, found.taken()) == (1, -1, {})


def test_the_work_with_the_oldest_run_is_forgotten_first(tmp_path: Path, monkeypatch) -> None:
    from halyard.workflows import runs

    kept = tmp_path / "workflow-runs.json"
    monkeypatch.setattr(runs, "WORKS", 2)

    for number, when in ((1, AT), (2, AT + timedelta(hours=1)), (3, AT + timedelta(hours=2))):
        save(kept, f"alpha-engine#{number}", a_run(1, since=when))

    assert current(kept, "alpha-engine#1") is None
    assert current(kept, "alpha-engine#3") is not None
