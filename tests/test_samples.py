"""The samples work as their READMEs say: somebody copies one and runs it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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


def unit_tested(where: Path) -> subprocess.CompletedProcess[str]:
    """The tests of a sample written in Python, as `make test-fast` runs them —
    without leaving `__pycache__` in the copy."""
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-q"],
        cwd=where,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
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


def test_the_packing_list_s_check_fails_until_the_task_is_done(tmp_path: Path) -> None:
    """Sample 04: what the driver is asked to add is missing to begin with."""
    copy = tmp_path / "packing-list"
    shutil.copytree(SAMPLES / "04-inspections", copy)

    first = checked(copy)
    page = copy / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            '<ul id="items">',
            '<input type="checkbox" id="hide-packed">\n<p id="left"></p>\n<ul id="items">',
        ),
        encoding="utf-8",
    )
    then = checked(copy)

    assert first.returncode == 1
    assert "MISSING #hide-packed" in first.stdout and "MISSING #left" in first.stdout
    assert then.returncode == 0


def test_the_shopping_cart_s_tests_fail_until_the_task_is_done(tmp_path: Path) -> None:
    """Sample 05: the discount test is there before the discount is, so
    `make test-fast` is red until the agent has done the task."""
    copy = tmp_path / "shopping-cart"
    shutil.copytree(SAMPLES / "05-the-whole-mechanism", copy)

    first = unit_tested(copy)
    (copy / "cart.py").write_text(
        "def total(items, discount=0):\n"
        "    return sum(p * q for p, q in items) * (100 - discount) // 100\n",
        encoding="utf-8",
    )
    then = unit_tested(copy)

    assert first.returncode == 1 and "test_a_discount_takes_a_percentage_off" in first.stderr
    assert then.returncode == 0, then.stderr


def test_the_cart_s_shipping_tests_fail_until_the_task_is_done(tmp_path: Path) -> None:
    """Sample 06: the shipping tests are there before `shipping()` is, so the
    `developed` transition reports them failing until the driver has done the
    task."""
    copy = tmp_path / "cart-transitions"
    shutil.copytree(SAMPLES / "06-transitions", copy)

    first = unit_tested(copy)
    with (copy / "cart.py").open("a", encoding="utf-8") as cart:
        cart.write("\n\ndef shipping(items):\n    return 0 if total(items) >= 5000 else 499\n")
    then = unit_tested(copy)

    assert first.returncode == 1 and "test_shipping_is_free_from_5000" in first.stderr
    assert "test_a_discount_takes_a_percentage_off" not in first.stderr
    assert then.returncode == 0, then.stderr


def test_the_cart_s_coupon_tests_fail_until_the_task_is_done(tmp_path: Path) -> None:
    """Sample 07: the coupon tests are there before `with_coupon()` is, so
    `developed` and `close` report them failing until the driver has done the
    task."""
    copy = tmp_path / "cart-workflow"
    shutil.copytree(SAMPLES / "07-a-workflow", copy)

    first = unit_tested(copy)
    with (copy / "cart.py").open("a", encoding="utf-8") as cart:
        cart.write(
            "\n\ndef with_coupon(items, code):\n"
            "    if code == 'SAVE10':\n"
            "        return total(items, discount=10)\n"
            "    if code == 'FIVEOFF':\n"
            "        return max(total(items) - 500, 0)\n"
            "    raise ValueError(code)\n"
        )
    then = unit_tested(copy)

    assert first.returncode == 1 and "test_fiveoff_never_goes_below_zero" in first.stderr
    assert "test_shipping_is_free_from_5000" not in first.stderr
    assert then.returncode == 0, then.stderr


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    """Sample 08, copied out as its README says."""
    if shutil.which("make") is None:
        pytest.skip("sample 08's checks run through make")
    copy = tmp_path / "shop"
    shutil.copytree(SAMPLES / "08-three-agents", copy)
    return copy


def validated(where: Path) -> subprocess.CompletedProcess[str]:
    """`test-fast` the way Halyard runs it: through scripts/validate.sh."""
    return subprocess.run(
        ["scripts/validate.sh", "test-fast"],
        cwd=where,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_the_shop_s_checks_fail_until_the_task_is_done(shop: Path) -> None:
    """Sample 08: the contract and both contexts' tests are there before the
    stock is, so `test-fast` is red until the driver has done the task — and
    says so on its last line, part by part, the line Halyard reads."""
    first = validated(shop)
    (shop / "catalog" / "products.py").write_text(
        'PRICES = {"tea": 450, "mug": 1200, "biscuits": 250}\n'
        'STOCK = {"tea": 10, "mug": 2, "biscuits": 0}\n',
        encoding="utf-8",
    )
    (shop / "catalog" / "__init__.py").write_text(
        "from catalog.products import PRICES, STOCK\n\n"
        '__all__ = ["UnknownProductError", "in_stock", "price_of"]\n\n\n'
        "class UnknownProductError(KeyError):\n    pass\n\n\n"
        "def price_of(sku):\n"
        "    if sku not in PRICES:\n        raise UnknownProductError(sku)\n"
        "    return PRICES[sku]\n\n\n"
        "def in_stock(sku):\n    price_of(sku)\n    return STOCK[sku]\n",
        encoding="utf-8",
    )
    (shop / "cart" / "__init__.py").write_text(
        "import catalog\n\n"
        '__all__ = ["Cart", "OutOfStockError"]\n\n\n'
        "class OutOfStockError(ValueError):\n    pass\n\n\n"
        "class Cart:\n"
        "    def __init__(self):\n        self.lines = {}\n\n"
        "    def add(self, sku, quantity=1):\n"
        "        wanted = self.lines.get(sku, 0) + quantity\n"
        "        if wanted > catalog.in_stock(sku):\n            raise OutOfStockError(sku)\n"
        "        self.lines[sku] = wanted\n\n"
        "    def total(self):\n"
        "        return sum(catalog.price_of(s) * q for s, q in self.lines.items())\n",
        encoding="utf-8",
    )
    then = validated(shop)

    assert first.returncode == 1
    assert first.stdout.splitlines()[-1] == (
        "test-fast: exit 1 — boundaries: every boundary kept (2 contexts, 3 files), exit 0; "
        "contract: FAILED (errors=1), exit 1; unit: not run (stopped after contract)"
    )
    assert then.returncode == 0, then.stdout
    assert then.stdout.splitlines()[-1].startswith("test-fast: exit 0 — boundaries: every")


def test_the_guard_names_a_crossing_and_test_fast_fails_on_it(shop: Path) -> None:
    """Sample 08: the shortcut the task tempts — the cart reading the catalog's
    own data — is named by file and line, before any test runs."""
    cart = shop / "cart" / "__init__.py"
    cart.write_text(
        cart.read_text(encoding="utf-8")
        + "\n\ndef prices():\n    return catalog.products.PRICES\n",
        encoding="utf-8",
    )

    result = validated(shop)

    assert result.returncode == 1
    assert "boundaries: 1 crossing: cart/__init__.py:" in result.stdout.splitlines()[-1]
    assert "reaches into catalog.products, which catalog does not offer, exit 1" in result.stdout
    assert "contract: not run (stopped after boundaries)" in result.stdout


@pytest.mark.parametrize(
    "broken",
    [
        # The catalog's public face is gone: there is nothing to check against.
        "catalog/__init__.py",
        # And a target the Makefile does not have.
        None,
    ],
)
def test_a_check_that_cannot_run_answers_2_which_halyard_reads_as_not_run(
    shop: Path, broken: str | None
) -> None:
    """Sample 08: `not_run: [2]` stops a transition there, rather than hand on
    checks that never ran as if they had passed or failed."""
    if broken:
        (shop / broken).unlink()
        result = validated(shop)
    else:
        result = subprocess.run(
            ["scripts/validate.sh", "test-slow"],
            cwd=shop,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.returncode == 2
    assert " exit 2 — " in result.stdout.splitlines()[-1]


def test_each_inspection_a_sample_names_is_in_it() -> None:
    """An inspection reads its text from the project when it runs; one the
    sample does not ship would fail the transition that names it."""
    import yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        shipped = yaml.safe_load((folder / "halyard.yaml.example").read_text(encoding="utf-8"))
        placed = _placed(shipped["projects"], folder)
        for name, block in shipped["projects"].items():
            for inspection, spec in (block.get("inspections") or {}).items():
                file = spec["file"] if isinstance(spec, dict) else spec
                assert (placed[name] / file).is_file(), f"{folder.name}: {inspection}"


def test_every_transition_a_sample_has_reaches_one_of_its_agents() -> None:
    """`to:` naming a role nobody in the project holds loads, and then has
    nowhere to go when somebody presses it from a phone."""
    from halyard.core.config_file import projects_from_yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        text = (folder / "halyard.yaml.example").read_text(encoding="utf-8")
        for project in projects_from_yaml(text):
            reachable = {seat.label for seat in project.seats} | {
                seat.role.value for seat in project.seats if seat.role
            }
            for name, transition in project.transitions.items():
                assert transition.to in reachable, f"{folder.name}: {name} → {transition.to}"


def test_every_workflow_step_a_sample_has_goes_to_one_agent_of_its_role() -> None:
    """A step that could reach two agents stops the run there, and one given to
    an agent of another role goes to the wrong agent in every run."""
    from halyard.core.config_file import projects_from_yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        text = (folder / "halyard.yaml.example").read_text(encoding="utf-8")
        for project in projects_from_yaml(text):
            for name, step in project.workflows.steps.items():
                to = project.transitions[step.transition].to
                wanted = step.seat or to
                reached = [
                    seat
                    for seat in project.seats
                    if wanted in (seat.label, seat.role.value if seat.role else None)
                ]
                assert len(reached) == 1, f"{folder.name}: {name} reaches {len(reached)}"
                [seat] = reached
                assert to in (seat.label, seat.role.value if seat.role else None), (
                    f"{folder.name}: {name} goes to {seat.label}, not to {to}"
                )


def test_the_last_step_of_each_workflow_a_sample_has_asks_for_done() -> None:
    """At a workflow's last step only `done` ends the run; a prompt there that
    asks for `forward` stops every run instead of ending it."""
    import yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        shipped = yaml.safe_load((folder / "halyard.yaml.example").read_text(encoding="utf-8"))
        placed = _placed(shipped["projects"], folder)
        for name, block in shipped["projects"].items():
            workflows = block.get("workflows") or {}
            steps = workflows.get("steps") or {}
            for flow, order in workflows.items():
                if flow in ("steps", "decisions", "phases"):
                    continue
                last = order[-1]
                transition = (steps.get(last) or {}).get("transition") or last
                prompt = block["transitions"][transition]["prompt"]
                text = (placed[name] / prompt).read_text(encoding="utf-8")
                assert "`done`" in text, f"{folder.name}: {flow} ends at {last} without done"


def test_every_effort_a_sample_s_inspections_ask_of_claude_code_is_one_it_takes() -> None:
    """An effort the runtime does not take is dropped, and the inspection runs
    at the model's own — the sample would say one thing and do another. A
    `gpt-…` model runs on Codex, whose efforts are its CLI's to say, and
    `halyard doctor` checks those against the CLI on the machine."""
    from halyard.agents import registry
    from halyard.agents.claude_code.runner import EFFORT_LEVELS
    from halyard.core.config_file import projects_from_yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        text = (folder / "halyard.yaml.example").read_text(encoding="utf-8")
        for project in projects_from_yaml(text):
            for name, choice in project.inspection_models.items():
                if choice.effort is not None and registry.for_model(choice.model) == "claude-code":
                    assert choice.effort in EFFORT_LEVELS, f"{folder.name}: {name}"


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


def test_each_script_a_sample_s_commands_run_is_in_it_and_can_be_run() -> None:
    """`/command` runs a line in the project as it is written; a script the
    sample does not ship, or ships without its mode, fails on the phone."""
    import yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        shipped = yaml.safe_load((folder / "halyard.yaml.example").read_text(encoding="utf-8"))
        placed = _placed(shipped["projects"], folder)
        for name, block in shipped["projects"].items():
            for command, spec in (block.get("commands") or {}).items():
                line = spec["run"] if isinstance(spec, dict) else spec
                program = line.split()[0] if isinstance(line, str) and line.strip() else ""
                # A program named by its path in the project — `./check.sh`,
                # `scripts/validate.sh` — rather than one found on PATH.
                if "/" in program and not program.startswith(("/", "~")):
                    script = placed[name] / program
                    assert script.is_file(), f"{folder.name}: {command}"
                    assert os.access(script, os.X_OK), f"{folder.name}: {command}"


def test_every_command_a_sample_trusts_under_runs_is_accepted() -> None:
    """An entry Halyard refuses still asks, and the README would promise a
    command with no card that comes as one."""
    from halyard.core.config_file import projects_from_yaml

    for folder in sorted(SAMPLES.glob("[0-9][0-9]-*")):
        text = (folder / "halyard.yaml.example").read_text(encoding="utf-8")
        for project in projects_from_yaml(text):
            assert not project.runs_refused, f"{folder.name}: {project.runs_refused}"


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
