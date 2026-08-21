"""Stage 0. The properties that matter are recoverability and honest verdicts."""

from __future__ import annotations

import pytest

from scripts.db import utcnow
from scripts.prefilter import (
    RULESET_LLM, RULESET_V1, apply, export_batch, import_verdicts, judge, summary,
)


def seed(conn, rows):
    for name, what in rows:
        conn.execute(
            "INSERT INTO accounts (name, name_normalized, source, status, fund, what,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, name.lower(), "vc", "new", "testfund", what, utcnow(), utcnow()),
        )


def test_keyword_pass_and_fail():
    assert judge("X", "We train foundation models").verdict == "pass"
    assert judge("Y", "Artisanal sourdough delivered weekly").verdict == "fail"


def test_no_description_is_unknown_not_fail():
    """A company with no marketing copy was not judged, not rejected."""
    assert judge("Z", None).verdict == "unknown"
    assert judge("Z", "").verdict == "unknown"


def test_verdict_records_the_text_it_judged(conn):
    seed(conn, [("Alpha", "Computer vision for warehouses")])
    apply(conn)
    row = conn.execute("SELECT * FROM accounts WHERE name='Alpha'").fetchone()
    assert row["prefilter"] == "pass"
    assert row["prefilter_rule"] == RULESET_V1
    assert "warehouses" in row["prefilter_evidence"]
    assert row["prefilter_at"]


def test_rerunnable_without_refetching(conn):
    """Stage 0 must be re-runnable with better rules from stored text alone."""
    seed(conn, [("Beta", "Game-ready 3D assets")])
    apply(conn)
    assert conn.execute("SELECT prefilter FROM accounts").fetchone()[0] == "fail"
    import_verdicts(conn, [{"id": 1, "verdict": "pass", "reason": "image-to-3D is an ML model"}])
    row = conn.execute("SELECT prefilter, prefilter_rule FROM accounts").fetchone()
    assert (row["prefilter"], row["prefilter_rule"]) == ("pass", RULESET_LLM)


def test_a_fail_is_a_verdict_not_a_deletion(conn):
    seed(conn, [("Gamma", "Sourdough")])
    apply(conn)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_only_unjudged_skips_already_judged(conn):
    seed(conn, [("Delta", "We train models"), ("Eps", "Sourdough")])
    apply(conn)
    conn.execute("UPDATE accounts SET what = 'now mentions machine learning' WHERE name='Eps'")
    apply(conn, only_unjudged=True)
    assert conn.execute("SELECT prefilter FROM accounts WHERE name='Eps'").fetchone()[0] == "fail"
    apply(conn, only_unjudged=False)
    assert conn.execute("SELECT prefilter FROM accounts WHERE name='Eps'").fetchone()[0] == "pass"


def test_export_batch_covers_fail_and_unknown(conn):
    seed(conn, [("A", "We train models"), ("B", "Sourdough"), ("C", None)])
    apply(conn)
    batch = export_batch(conn, fund="testfund")
    assert {b["name"] for b in batch} == {"B", "C"}
    assert all("id" in b and "description" in b for b in batch)


def test_import_rejects_an_unknown_verdict(conn):
    seed(conn, [("A", "Sourdough")])
    apply(conn)
    with pytest.raises(ValueError, match="not one of"):
        import_verdicts(conn, [{"id": 1, "verdict": "maybe"}])


def test_import_rejects_a_pass_with_no_reason(conn):
    """A pass spends a research budget; it has to say what it saw."""
    seed(conn, [("A", "Sourdough")])
    apply(conn)
    with pytest.raises(ValueError, match="needs a reason"):
        import_verdicts(conn, [{"id": 1, "verdict": "pass"}])


def test_import_rejects_a_missing_id(conn):
    with pytest.raises(ValueError, match="integer id"):
        import_verdicts(conn, [{"verdict": "fail"}])


def test_excluded_accounts_are_not_judged(conn):
    seed(conn, [("A", "We train models")])
    conn.execute("UPDATE accounts SET status='excluded'")
    assert sum(apply(conn).values()) == 0


def test_summary_reports_the_pass_share():
    out = summary({"pass": 2, "fail": 6, "unknown": 2})
    assert "20%" in out and "10 accounts" in out
