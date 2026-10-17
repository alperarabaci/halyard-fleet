"""The samples work as their READMEs say: somebody copies one and runs it."""

from __future__ import annotations

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
