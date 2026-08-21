"""Guards for the failures that were silent the first time.

Every check here replaced something the operator had to remember to pass. A fix
that depends on remembering is not a fix: these all failed quietly once, and a
quiet failure is not noticed three weeks later. Each one is now either the
default or a hard failure, and this file is what keeps it that way.
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from scripts.outbound import app

runner = CliRunner()


def test_person_pages_refuses_without_a_company():
    """Corroboration is what separates a person's page from a namesake's."""
    r = runner.invoke(app, ["person-pages", "Some Name"])
    assert r.exit_code != 0
    assert "--company" in r.output


def test_search_is_the_default_in_the_probe_not_a_flag(monkeypatch):
    """Guessing misses handles no rule derives. Search has to be the default
    path, not something recalled at the moment it would have helped."""
    import inspect

    from scripts.person_pages import find

    assert inspect.signature(find).parameters["search_first"].default is True

    called = {}

    def _spy(name, company=None):
        called["hit"] = True
        return []

    monkeypatch.setattr("scripts.person_pages.discovered_urls", _spy)
    monkeypatch.setattr("scripts.person_pages.fetch_one",
                        lambda url, timeout=20: (_ for _ in ()).throw(StopIteration))
    try:
        find("A B", company="C")
    except StopIteration:
        pass
    assert called.get("hit"), "search was not consulted before guessing"


def test_company_resolve_refuses_to_register_without_routing(tmp_path):
    """An unroutable account ingests contacts that no campaign review can see
    and no send can reach, and nothing anywhere reports an error."""
    db = str(tmp_path / "t.db")
    r = runner.invoke(app, ["company-resolve", "Nonesuch Labs",
                            "--domain", "example.invalid", "--db", db])
    assert r.exit_code == 3
    assert "REFUSING" in r.output

    from scripts.db import open_db
    assert open_db(db).execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_outbound_db_env_var_redirects(tmp_path, monkeypatch):
    """One variable for a whole test run, because --db on every command is a
    step that can be half-applied -- and the half that misses writes to
    production while the run looks fine."""
    from scripts.db import open_db

    target = tmp_path / "scratch.db"
    monkeypatch.setenv("OUTBOUND_DB", str(target))
    open_db().execute("SELECT 1")
    assert target.exists()


def test_candidate_evidence_url_must_be_absolute():
    """source_url on every evidence item, enforced by the model rather than by
    whoever wrote the candidate file."""
    from pydantic import ValidationError

    from scripts.candidates import Evidence

    with pytest.raises(ValidationError):
        Evidence(claim="x works at Y as Z", url="/relative", quote="q",
                 retrieved_at="2026-08-21T00:00:00Z")
