"""Tests for the reads that go through without a card.

Weighted the way `test_writes.py` is: every way through that was found — by a
review, by measuring, by reading the old rules — is a case here, because a
false grant hands an agent something nobody saw, and a false card costs a tap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from halyard.core.reads import judge


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "src" / "a" / "b").mkdir(parents=True)
    (root / "NOTES").mkdir()
    (root / "src" / "x.py").write_text("x = 1\n")
    (root / "NOTES" / "x.json").write_text("{}\n")
    (root / ".env").write_text("TOKEN=secret\n")
    (root / ".env.example").write_text("TOKEN=\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "passwd").write_text("root\n")
    return root


def allowed(command: str, project: Path, cwd: Path | str | None = None) -> bool:
    return judge(command, cwd=str(cwd or project), project=str(project)).allowed


# --- what goes through ---------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git grep -n foo",
        "grep -rn TODO src | head -20",
        "sed -n '1,40p' src/x.py",
        "rg -n x src 2>/dev/null | head",
        "git log --oneline -5",
        "git status && git diff --stat",
        "ls -la",
        "cat src/x.py | wc -l",
        'grep -n "->" src/x.py',
        "find . -name '*.py' -maxdepth 2",
        "[ -d src ] && ls src",
        'echo "---"; git branch --show-current',
        "wc -l < src/x.py",
        "git show HEAD:src/x.py",
        "jq '.a' NOTES/x.json",
        "sort -k2 src/x.py | uniq -c",
        "LC_ALL=C sort src/x.py",
        "cat .env.example",
    ],
)
def test_a_read_inside_the_project_goes_through(command: str, project: Path) -> None:
    assert allowed(command, project)


def test_a_cd_into_the_project_is_followed(project: Path) -> None:
    """How one runtime wraps every command: 2346 of them in two weeks."""
    assert allowed(f'cd "{project}"\ngrep -n thing .', project)
    assert allowed(f"cd {project} && git log -3", project)


def test_the_reason_names_what_ran(project: Path) -> None:
    """Written into the audit record, so a grant can be read back later."""
    verdict = judge("git grep -n x | head -3", cwd=str(project), project=str(project))

    assert verdict.why == "a read inside the project: git grep, head"


@pytest.mark.parametrize(
    "command",
    ["ls\ngit status", "ls; git status", "ls && git status", "ls || git status", "ls | wc -l"],
)
def test_every_separator(command: str, project: Path) -> None:
    assert allowed(command, project)


@pytest.mark.parametrize(
    "command",
    [
        "cat src/x.py 2>&1 | head",
        "cat src/x.py >/dev/null",
        "cat src/x.py 2>/dev/null",
        "cat src/x.py &>/dev/null",
        "echo done >&2",
    ],
)
def test_a_redirect_that_writes_nothing_is_not_a_write(command: str, project: Path) -> None:
    """1446 cards over two weeks were a read with `2>/dev/null` on the end, and
    5,093 medium labels came from a rule that read `2>&1` as a file write."""
    assert allowed(command, project)


@pytest.mark.parametrize(
    "command",
    ["grep 'x$' src/x.py", 'grep -n "\\$var" src/x.py', "echo cost: $", "echo '$HOME'"],
)
def test_a_dollar_that_expands_nothing_is_text(command: str, project: Path) -> None:
    assert allowed(command, project)


# --- the ways through that were found ------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # One read made the whole command low under the old rules.
        "ls | xargs rm",
        "echo ok; python3 x.py",
        "cat src/x.py && ./run.sh",
        "find . -name '*.pyc' -delete",
        "git branch -D feature",
        # A single part that runs a program.
        "awk 'BEGIN {system(\"python3 x.py\")}'",
        "awk '{print $1}' src/x.py",
        "find . -exec rm {} \\;",
        "rg --pre cat x",
        "git -c core.pager=x log",
        "sed 's/a/b/e' src/x.py",
        "jq -n env",
        # Hidden from the old judge by redaction.
        "TOKEN=$(python3${IFS}x.py) git status",
        # Out of the project.
        "cat ../outside/passwd",
        "cd /tmp && cat outside.txt",
        "ls ~",
        "cat /etc/*",
        "git -C /tmp log",
        # Writes.
        "ls > out.txt",
        "echo hi >> NOTES/x.md",
        "sort -o out.txt src/x.py",
        "git diff --output=/tmp/x",
        "sed -i 's/a/b/' src/x.py",
        "sed 's/a/b/w out' src/x.py",
        # Code, which a read is not: test runs come back as project permissions.
        "uv run pytest -q 2>&1 | tail -5",
        "python3 -c 'print(1)'",
    ],
)
def test_a_way_through_is_a_card(command: str, project: Path) -> None:
    assert not allowed(command, project)


@pytest.mark.parametrize(
    "command",
    ["echo $TOKEN", 'echo "$TOKEN"', "echo ${TOKEN}", "ls `pwd`", "diff <(ls) <(ls src)"],
)
def test_an_expansion_is_not_understood(command: str, project: Path) -> None:
    """What `$` or a backtick becomes is only known when it runs."""
    assert not allowed(command, project)


@pytest.mark.parametrize(
    "command",
    ["cat <<EOF\nhi\nEOF", "cat <<< hi", "(ls)", "{ ls; }", "ls &", "! ls"],
)
def test_a_heredoc_a_group_or_the_background_is_not_understood(command: str, project: Path) -> None:
    assert not allowed(command, project)


# --- paths ---------------------------------------------------------------------


def test_a_symlink_out_of_the_project_is_outside(project: Path, tmp_path: Path) -> None:
    """Resolved before it is judged, like every path in `writes.py`."""
    (project / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)

    assert not allowed("cat escape/passwd", project)


def test_a_cd_that_may_not_have_happened_is_judged_from_both(project: Path) -> None:
    """If the `cd` fails, what follows runs where it started. From `src/a/b`
    the path is inside; from the project it is not — and even `&&` is no
    guarantee once a `;` follows it."""
    assert not allowed("cd src/a/b; cat ../../../../outside/passwd", project)
    assert not allowed("cd src/a/b && true; cat ../../../../outside/passwd", project)
    assert not allowed("cd src/a/b || cat ../../../../outside/passwd", project)


def test_running_outside_the_project_lets_nothing_through(project: Path, tmp_path: Path) -> None:
    assert not allowed("ls", project, cwd=tmp_path / "outside")


def test_no_project_means_no_grant(project: Path) -> None:
    assert not judge("ls", cwd=str(project), project=None).allowed


def test_a_file_that_may_hold_a_secret_asks_even_inside(project: Path) -> None:
    assert not allowed("cat .env", project)
    assert not allowed("grep TOKEN .env", project)
    assert not allowed("cd src && cat ../.env", project)


def test_a_glob_that_reaches_a_secret_asks(project: Path) -> None:
    """Expanded before it is judged: `.*` names `.env`."""
    assert not allowed("cat .*", project)
    assert allowed("cat src/*.py", project)


def test_a_template_is_not_a_secret(project: Path) -> None:
    """`.env.example` is committed so people know which settings exist."""
    assert allowed("cat .env.example", project)


def test_a_repository_above_the_project_is_not_read(tmp_path: Path) -> None:
    """Git reads the repository it finds, not the project. A home directory
    kept in git would hand every file in it to `git show`."""
    above = tmp_path / "home"
    (above / ".git").mkdir(parents=True)
    inner = above / "work"
    inner.mkdir()

    assert not allowed("git log -3", inner)
    assert allowed("ls", inner)


# --- options -------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", ["sort --out=x src/x.py", "git log --outp=/tmp/x", "sed --in-pl src/x.py"]
)
def test_a_long_option_cut_short_is_still_that_option(command: str, project: Path) -> None:
    """GNU tools and git take any unambiguous start of a long option."""
    assert not allowed(command, project)


@pytest.mark.parametrize(
    ("command", "through"),
    [
        ("sed -n '1,40p' src/x.py", True),
        ("sed -n '/x/p' src/x.py", True),
        ("sed 's/a/b/g' src/x.py", True),
        ("sed 20q src/x.py", True),
        ("sed -n '1p;5p' src/x.py", True),
        ("sed -ni 's/a/b/' src/x.py", False),
        ("sed -f script.sed src/x.py", False),
        ("sed 'y/ab/cd/' src/x.py", False),
        ("sed '1r /etc/passwd' src/x.py", False),
    ],
)
def test_sed_only_prints(command: str, through: bool, project: Path) -> None:
    assert allowed(command, project) is through


@pytest.mark.parametrize(
    ("command", "through"),
    [
        ("find src -type f", True),
        ("find . -name x -newer src/x.py", True),
        ("find -H /etc -name passwd", False),
        ("find . -newer /etc/passwd", False),
        ("find . -fprint out.txt", False),
        ("find -L . -name x", False),
    ],
)
def test_find_starts_nowhere_outside_and_does_nothing(
    command: str, through: bool, project: Path
) -> None:
    assert allowed(command, project) is through


@pytest.mark.parametrize(
    ("command", "through"),
    [
        ("git diff HEAD~1 --stat", True),
        ("git branch -vv", True),
        ("git branch --contains HEAD", True),
        ("git tag -l 'v*'", True),
        ("git stash list", True),
        ("git remote -v", True),
        ("git reflog", True),
        ("git commit -m x", False),
        ("git push", False),
        ("git checkout main", False),
        ("git branch new-name", False),
        ("git tag v9", False),
        ("git stash", False),
        ("git config --list", False),
        ("git diff --no-index /etc/passwd x", False),
        ("git grep -O x", False),
        ("git fetch", False),
    ],
)
def test_git_reads_and_nothing_else(command: str, through: bool, project: Path) -> None:
    assert allowed(command, project) is through


@pytest.mark.parametrize(
    ("command", "through"),
    [
        ("rg -tpy x src", True),
        ("rg --files src", True),
        ("rg -uu TOKEN", False),
        ("rg --no-ignore-vcs x", False),
        ("rg -L x", False),
        ("rg --hostname-bin x y", False),
        ("grep -R x src", False),
        ("grep -f /etc/passwd x src", False),
        ("grep -rnf/etc/passwd src", False),
        ("grep -rnf ../outside/passwd src", False),
    ],
)
def test_searches_run_nothing_and_read_nothing_outside(
    command: str, through: bool, project: Path
) -> None:
    assert allowed(command, project) is through


@pytest.mark.parametrize(
    ("command", "through"),
    [
        ("LC_ALL=C sort src/x.py", True),
        ("GIT_EXTERNAL_DIFF=x git diff", False),
        ("PAGER=x git log", False),
        ("PATH=/tmp/evil; ls", False),
    ],
)
def test_the_environment_a_read_may_run_with(command: str, through: bool, project: Path) -> None:
    """`GIT_EXTERNAL_DIFF` runs a program; `PATH` changes which one runs."""
    assert allowed(command, project) is through


def test_jq_reads_no_environment_and_no_file_outside(project: Path) -> None:
    assert not allowed("jq -n '$ENV.TOKEN'", project)
    assert not allowed("jq --rawfile t /etc/passwd -n '$t'", project)
    assert allowed("jq --arg k v '.[$k]' NOTES/x.json", project)


def test_set_may_change_how_errors_stop_but_not_print_everything(project: Path) -> None:
    assert allowed("set -euo pipefail; git status", project)
    assert not allowed("set", project)


def test_a_program_named_by_its_path_is_not_a_read(project: Path) -> None:
    assert not allowed("./cat src/x.py", project)
    assert not allowed("/bin/cat src/x.py", project)
