"""The example configurations load: a copy of one is where somebody starts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from halyard.config import Settings
from halyard.core.config_file import projects_from_yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["halyard.simple.yaml.example", "halyard.yaml.example"])
def test_an_example_configuration_loads(name: str, tmp_path: Path, monkeypatch) -> None:
    """Its settings and its projects both, read the way the control plane reads
    the copy somebody makes of it."""
    shutil.copyfile(ROOT / name, tmp_path / "halyard.yaml")
    monkeypatch.chdir(tmp_path)

    settings = Settings()
    [project, *_] = projects_from_yaml((tmp_path / "halyard.yaml").read_text(encoding="utf-8"))

    assert settings.telegram_bot_token
    assert project.path is not None
    assert project.seats
