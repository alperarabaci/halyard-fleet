"""The samples work as their READMEs say: somebody copies one and runs it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def test_every_sample_the_index_links_to_is_there() -> None:
    """A link to a sample that is not there is a dead end for the one reader
    who followed it. Planned ones are listed without a link."""
    for index in (SAMPLES / "README.md", ROOT / "README.md"):
        text = index.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(((?:samples/)?\d\d-[^)/]+)/?\)", text):
            folder = (index.parent / target).resolve()
            assert (folder / "README.md").is_file(), f"{index.name} links to {target}"


def test_each_sample_has_its_parts() -> None:
    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        for part in ("README.md", "halyard.yaml.example", "PROMPT.md"):
            assert (folder / part).is_file(), f"{folder.name} has no {part}"


@pytest.fixture
def counter(tmp_path: Path) -> Path:
    """The first sample, copied out as its README says."""
    copy = tmp_path / "hello-counter"
    shutil.copytree(SAMPLES / "01-one-agent", copy)
    return copy


def _placed(blocks: dict, folder: Path) -> dict[str, Path]:
    from halyard import samples_cli

    return samples_cli._placed(blocks, folder)


def checked(where: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["./check.sh"], cwd=where, capture_output=True, text=True, timeout=10, check=False
    )


def test_the_counter_s_check_fails_until_the_task_is_done(counter: Path) -> None:
    """What the agent is asked to fix is missing to begin with."""
    result = checked(counter)

    assert result.returncode == 1
    assert "MISSING #reset" in result.stdout


def test_the_counter_s_check_passes_once_a_reset_button_is_there(counter: Path) -> None:
    page = counter / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            '<button id="add"', '<button id="reset" type="button">Reset</button>\n<button id="add"'
        ),
        encoding="utf-8",
    )

    result = checked(counter)

    assert result.returncode == 0
    assert "ok   #reset" in result.stdout


@pytest.mark.parametrize(
    ("product", "missing", "before"),
    [
        ("greeting-page", "name", "</h1>"),
        ("todo-list", "clear", '<ul id="items">'),
    ],
)
def test_the_two_products_checks_fail_until_their_tasks_are_done(
    tmp_path: Path, product: str, missing: str, before: str
) -> None:
    """Sample 02: each product's task is what its check says is missing."""
    copy = tmp_path / "two-agents"
    shutil.copytree(SAMPLES / "02-two-agents", copy)
    where = copy / product

    first = checked(where)
    page = where / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            before, f'{before}\n<button id="{missing}" type="button"></button>', 1
        ),
        encoding="utf-8",
    )
    then = checked(where)

    assert first.returncode == 1 and f"MISSING #{missing}" in first.stdout
    assert then.returncode == 0 and f"ok   #{missing}" in then.stdout


def test_the_reading_list_s_check_fails_until_the_task_is_done(tmp_path: Path) -> None:
    """Sample 03: what the driver is asked to add is missing to begin with."""
    copy = tmp_path / "reading-list"
    shutil.copytree(SAMPLES / "03-handing-work-over", copy)

    first = checked(copy)
    page = copy / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            '<ul id="books"></ul>',
            '<input id="filter">\n<ul id="books"></ul>\n<p id="shown"></p>',
        ),
        encoding="utf-8",
    )
    then = checked(copy)

    assert first.returncode == 1
    assert "MISSING #filter" in first.stdout and "MISSING #shown" in first.stdout
    assert then.returncode == 0


def test_each_transition_prompt_a_sample_names_is_in_it() -> None:
    """A transition reads its prompt from the project when it goes; one the
    sample does not ship would arrive with no prompt in front."""
    import yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        shipped = yaml.safe_load((folder / "halyard.yaml.example").read_text(encoding="utf-8"))
        placed = _placed(shipped["projects"], folder)
        for name, block in shipped["projects"].items():
            for transition, spec in (block.get("transitions") or {}).items():
                for key in ("prompt", "followup_prompt"):
                    if spec.get(key):
                        assert (placed[name] / spec[key]).is_file(), f"{folder.name}: {transition}"


def test_each_sample_lays_its_projects_out_as_its_example_says() -> None:
    """`halyard samples add` finds each project in the copy where the example
    puts it; a folder missing from the sample is a step its README cannot get
    past."""
    import yaml

    from halyard import samples_cli

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        shipped = yaml.safe_load((folder / "halyard.yaml.example").read_text(encoding="utf-8"))
        for name, where in samples_cli._placed(shipped["projects"], folder).items():
            assert where.is_dir(), f"{folder.name}: {name} has no folder"


def test_every_check_a_sample_runs_can_be_run() -> None:
    """`./check.sh` is what the agents and `/command` run; a copy keeps the mode
    git gave it."""
    for check in sorted(SAMPLES.rglob("check.sh")):
        assert os.access(check, os.X_OK), check.relative_to(SAMPLES)


def test_each_sample_s_prompt_is_its_task_word_for_word() -> None:
    """The phone's `/sample_NN` and PROMPT.md are the same task; one edited
    without the other would send something nobody reads in the README."""
    import yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        shipped = yaml.safe_load((folder / "halyard.yaml.example").read_text(encoding="utf-8"))
        prompt = (shipped.get("prompts") or {}).get(f"sample_{folder.name[:2]}")
        assert prompt, f"{folder.name} has no /sample_{folder.name[:2]}"
        task = (folder / "PROMPT.md").read_text(encoding="utf-8")
        assert " ".join(prompt.split()) == " ".join(task.split()), folder.name
