"""Fixtures only. No test in this suite touches the network."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scripts.config import Config
from scripts.db import connect, migrate

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example"

# Belt and braces alongside the PYTEST_CURRENT_TEST check in db.connect: no test
# may reach the production database, whatever it forgets to pass.
os.environ["OUTBOUND_NO_PROD_DB"] = "1"


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    """A throwaway config directory built from config.example.

    Using the shipped example rather than a bespoke fixture means the tests fail
    if the example stops being loadable, which is the thing a new user hits first.
    """
    root = tmp_path / "config"
    shutil.copytree(EXAMPLE, root)

    # Attachment files must exist for the cross-file check to pass.
    attach = tmp_path / "attachments"
    for subdir, names in (
        ("_first_email", ["example_document_a.pdf", "example_document_b.pdf"]),
        ("_second_email", ["example_preprint_a.pdf", "example_preprint_b.pdf"]),
    ):
        (attach / subdir).mkdir(parents=True, exist_ok=True)
        for n in names:
            (attach / subdir / n).write_bytes(b"%PDF-1.4 fixture\n")

    campaign = (root / "campaign.yaml").read_text().replace(
        "attachments_root: /path/to/your/attachments", f"attachments_root: {attach}"
    )
    (root / "campaign.yaml").write_text(campaign)
    return root


@pytest.fixture
def config(config_root: Path) -> Config:
    return Config(config_root)


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "test.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def candidates_dir() -> Path:
    return ROOT / "tests" / "fixtures" / "candidates"
