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


def test_resolve_and_ingest_agree_on_the_account_key(tmp_path):
    """company-resolve looked the account up by LOWER(name) while ingest keys on
    normalize_company. The two never matched, so ingest created a second row and
    the routing written to the first never reached the contacts -- they landed
    with campaign NULL and were invisible to the review export. Same silent
    shape as the original routing bug, one layer down."""
    from scripts.db import open_db
    from scripts.ingest_candidates import upsert_account
    from scripts.normalize import normalize_company

    db = str(tmp_path / "t.db")
    r = runner.invoke(app, ["company-resolve", "Together AI", "--domain", "together.ai",
                            "--tier", "startup", "--db", db])
    assert r.exit_code == 0, r.output

    conn = open_db(db)
    key = conn.execute("SELECT name_normalized FROM accounts").fetchone()[0]
    assert key == normalize_company("Together AI")

    class CF:
        company, domain, status, searches_used, budget_exhausted = \
            "Together AI", "together.ai", "done", 0, False

    assert upsert_account(conn, CF(), "test", "ref") == 1
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_leadership_ranks_below_plain_ic_titles():
    """Inverse of an org chart, deliberately. A co-founder is the profile least
    likely to reply: full inbox, and an outside collaboration routes through a
    process rather than being a personal yes."""
    from scripts import seniority

    assert seniority.rank("Research Scientist") < seniority.rank("Principal Engineer")
    assert seniority.rank("Principal Engineer") < seniority.rank("Co-founder")
    assert seniority.rank("Member of Technical Staff") == seniority.IC_RESEARCH
    for t in ("Co-founder and Chief Scientist", "Head of Research", "VP of Engineering",
              "Director of ML", "CTO"):
        assert seniority.is_leadership(t), t
    for t in ("AI Researcher", "Applied Scientist", "Research Engineer"):
        assert not seniority.is_leadership(t), t


def test_leadership_is_flagged_not_excluded():
    """'Don't hard-exclude them' -- they must still be reachable, just deliberate."""
    from scripts.review import risk_flags

    row = {"email_basis": "observed", "email_pattern_samples": 0,
           "email_pattern_confidence": 0, "observed_at": None,
           "verification_status": "mx_only", "personalization": "x",
           "personalization_source_url": "https://x.test", "liveness_status": None,
           "name": "A Founder", "email": "a@b.test", "account_domain": "b.test",
           "title": "Co-founder and Chief Scientist"}
    assert any("least likely to reply" in f for f in risk_flags(row))
    assert not risk_flags({**row, "title": "Research Scientist", "name": "B IC"})


def test_free_mail_is_dropped_only_when_inferred():
    """An inferred consumer address is a guess -- there is no pattern to infer
    from at gmail.com. One the person published on their own homepage as their
    contact is first-party and current. Conflating the two took Hugging Face
    from 15 addresses to 1."""
    from scripts.normalize import is_free_mail

    assert is_free_mail("gmail.com")
    src = (__import__("pathlib").Path("scripts/ingest_candidates.py")).read_text()
    assert 'is_free_mail(domain_of(email)) and c.email_basis != "observed"' in src


def test_reversed_name_order_is_one_person():
    """OpenAlex carries both orders for the same author. The exact-string merge
    missed it and put Hugging Face's CSO in the queue twice."""
    import re

    def key(n):
        return " ".join(sorted(t for t in re.split(r"[^a-z]+", n.strip().lower()) if t))

    assert key("Thomas Wolf") == key("Wolf Thomas")
    assert key("Edward Beeching") != key("Ed Beeching")   # diminutives remain open


def test_hf_org_membership_matches_reversed_names(monkeypatch):
    """OpenAlex hands back both name orders, and the verification oracle has to
    agree with itself about who a person is."""
    from scripts import hf_org

    monkeypatch.setattr(hf_org, "members", lambda slug: [
        {"name": "Thomas Wolf", "user": "thomwolf", "key": hf_org.name_key("Thomas Wolf")}])
    ok, why = hf_org.check("Hugging Face", "Wolf Thomas")
    assert ok is True and "thomwolf" in why


def test_an_unknown_org_is_unknown_not_absent(monkeypatch):
    """A company with no mapped org must not read as 'nobody works there'. The
    question could not be asked, which is a different answer from no."""
    from scripts import hf_org

    ok, why = hf_org.check("Some Company With No Org", "A Person")
    assert ok is None and "no Hugging Face org" in why


def test_a_departed_employee_fails_the_check(monkeypatch):
    """9 of 15 Hugging Face addresses belonged to people who had left."""
    from scripts import hf_org

    monkeypatch.setattr(hf_org, "members", lambda slug: [
        {"name": "Still Here", "user": "sh", "key": hf_org.name_key("Still Here")}])
    ok, why = hf_org.check("Hugging Face", "Douwe Kiela")
    assert ok is False and "out of date" in why
