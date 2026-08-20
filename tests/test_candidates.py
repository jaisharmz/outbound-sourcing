"""The validator is the structural guarantee that an ungrounded claim cannot
reach a send queue. Each test here is a way that guarantee could be lost."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.candidates import CandidateError, filter_suppressed, validate_file


def write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "company.json"
    p.write_text(json.dumps(payload))
    return p


def base_record(**over) -> dict:
    rec = {
        "name": "Ada Lovelace",
        "title": "Research Scientist",
        "company": "Northwind Labs",
        "email": "ada@northwindlabs.test",
        "email_basis": "observed",
        "evidence": [
            {
                "claim": "Ada Lovelace works at Northwind Labs as a Research Scientist",
                "url": "https://northwindlabs.test/team",
                "quote": "Ada Lovelace - Research Scientist",
                "retrieved_at": "2026-08-20T16:41:00Z",
            },
            {
                "claim": "email is ada@northwindlabs.test",
                "url": "https://arxiv.org/abs/0000.00001",
                "quote": "Ada Lovelace (ada@northwindlabs.test)",
                "retrieved_at": "2026-08-20T16:44:00Z",
            },
        ],
        "personalization": None,
        "personalization_source_url": None,
        "confidence": 0.9,
    }
    rec.update(over)
    return rec


def base_file(*records) -> dict:
    return {
        "company": "Northwind Labs",
        "domain": "northwindlabs.test",
        "generated_at": "2026-08-20T17:00:00Z",
        "candidates": list(records),
    }


def test_valid_record_passes(tmp_path):
    cf = validate_file(write(tmp_path, base_file(base_record())))
    assert cf.candidates[0].email == "ada@northwindlabs.test"


def test_rejects_evidence_without_a_url(tmp_path):
    rec = base_record()
    del rec["evidence"][1]["url"]
    with pytest.raises(CandidateError, match="url"):
        validate_file(write(tmp_path, base_file(rec)))


def test_rejects_relative_evidence_url(tmp_path):
    rec = base_record()
    rec["evidence"][1]["url"] = "/team"
    with pytest.raises(CandidateError, match="absolute http"):
        validate_file(write(tmp_path, base_file(rec)))


def test_rejects_ungrounded_email(tmp_path):
    """Two URLs on the record is not the same as the email being grounded."""
    rec = base_record()
    rec["evidence"][1] = {
        "claim": "the company was founded in 2024",
        "url": "https://northwindlabs.test/about",
        "quote": "Founded in 2024.",
        "retrieved_at": "2026-08-20T16:44:00Z",
    }
    with pytest.raises(CandidateError, match="grounds the email"):
        validate_file(write(tmp_path, base_file(rec)))


def test_rejects_ungrounded_identity(tmp_path):
    rec = base_record()
    rec["evidence"][0] = {
        "claim": "the page lists several people",
        "url": "https://northwindlabs.test/team",
        "quote": "Our team",
        "retrieved_at": "2026-08-20T16:41:00Z",
    }
    with pytest.raises(CandidateError, match="binds"):
        validate_file(write(tmp_path, base_file(rec)))


def test_rejects_personalization_without_a_source_url(tmp_path):
    rec = base_record(personalization="I enjoyed your recent paper on retrieval.")
    with pytest.raises(CandidateError, match="personalization_source_url"):
        validate_file(write(tmp_path, base_file(rec)))


def test_rejects_personalization_that_is_not_a_sentence(tmp_path):
    rec = base_record(
        personalization="your paper on retrieval",
        personalization_source_url="https://arxiv.org/abs/0000.00001",
    )
    with pytest.raises(CandidateError, match="complete sentences"):
        validate_file(write(tmp_path, base_file(rec)))


def test_accepts_null_personalization(tmp_path):
    cf = validate_file(write(tmp_path, base_file(base_record(personalization=None))))
    assert cf.candidates[0].personalization is None


def test_rejects_malformed_email(tmp_path):
    for bad in ("ada@@northwindlabs.test", "ada at northwindlabs.test", "ada@localhost"):
        rec = base_record(email=bad)
        with pytest.raises(CandidateError):
            validate_file(write(tmp_path, base_file(rec)))


def test_rejects_unknown_field(tmp_path):
    rec = base_record(seniority="senior")
    with pytest.raises(CandidateError, match="[Ee]xtra"):
        validate_file(write(tmp_path, base_file(rec)))


def test_rejects_suppressed_address_at_the_gate(tmp_path):
    path = write(tmp_path, base_file(base_record()))
    with pytest.raises(CandidateError, match="suppression list"):
        validate_file(path, suppressed={"ada@northwindlabs.test"})


def test_filter_suppressed_drops_rather_than_rejects(tmp_path):
    cf = validate_file(write(tmp_path, base_file(base_record())))
    cf, dropped = filter_suppressed(cf, {"northwindlabs.test"})
    assert dropped == ["ada@northwindlabs.test"]
    assert cf.candidates == []


def test_empty_file_requires_a_reason(tmp_path):
    payload = base_file()
    with pytest.raises(CandidateError, match="reason"):
        validate_file(write(tmp_path, payload))


def test_empty_file_with_a_reason_is_valid(tmp_path):
    payload = base_file()
    payload["reason"] = "No team page and no grounded emails found."
    assert validate_file(write(tmp_path, payload)).candidates == []


def test_budget_exhausted_marks_the_company_degraded(tmp_path):
    payload = base_file(base_record())
    payload["budget_exhausted"] = True
    payload["searches_used"] = 15
    assert validate_file(write(tmp_path, payload)).status == "degraded"


def test_clean_run_is_done(tmp_path):
    assert validate_file(write(tmp_path, base_file(base_record()))).status == "done"


def test_honorific_is_stripped_from_the_salutation(tmp_path):
    """A template must never render "Hello Dr.!"."""
    rec = base_record(name="Dr. Ada Q. Lovelace")
    rec["evidence"][0]["claim"] = "Ada Lovelace works at Northwind Labs as a Research Scientist"
    cf = validate_file(write(tmp_path, base_file(rec)))
    assert cf.candidates[0].first_name == "Ada"
    assert cf.candidates[0].last_name == "Lovelace"


def test_legal_suffix_does_not_break_identity_grounding(tmp_path):
    """Evidence naming "Kepler Systems" grounds a record for "Kepler Systems, Inc."."""
    rec = base_record(company="Kepler Systems, Inc.", email="alan@keplersystems.test")
    rec["name"] = "Alan Turing"
    rec["evidence"][0] = {
        "claim": "Alan Turing is a founder at Kepler Systems",
        "url": "https://keplersystems.test/about",
        "quote": "Founded in 2024 by Alan Turing.",
        "retrieved_at": "2026-08-20T17:01:00Z",
    }
    rec["evidence"][1] = {
        "claim": "email is alan@keplersystems.test",
        "url": "https://github.com/example/kepler/commit/abc",
        "quote": "Author: Alan Turing <alan@keplersystems.test>",
        "retrieved_at": "2026-08-20T17:02:00Z",
    }
    payload = base_file(rec)
    payload["company"] = "Kepler Systems, Inc."
    assert validate_file(write(tmp_path, payload)).candidates[0].first_name == "Alan"
