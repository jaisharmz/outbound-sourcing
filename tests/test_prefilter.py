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
    # keywords_v1 cannot tell depth apart, so its pass is the conservative one.
    assert judge("X", "We train foundation models").verdict == "pass_applies"
    assert judge("Y", "Artisanal sourdough delivered weekly").verdict == "fail"


def test_no_description_is_unknown_not_fail():
    """A company with no marketing copy was not judged, not rejected."""
    assert judge("Z", None).verdict == "unknown"
    assert judge("Z", "").verdict == "unknown"


def test_verdict_records_the_text_it_judged(conn):
    seed(conn, [("Alpha", "Computer vision for warehouses")])
    apply(conn)
    row = conn.execute("SELECT * FROM accounts WHERE name='Alpha'").fetchone()
    assert row["prefilter"] == "pass_applies"
    assert row["prefilter_rule"] == RULESET_V1
    assert "warehouses" in row["prefilter_evidence"]
    assert row["prefilter_at"]


def test_rerunnable_without_refetching(conn):
    """Stage 0 must be re-runnable with better rules from stored text alone."""
    seed(conn, [("Beta", "Game-ready 3D assets")])
    apply(conn)
    assert conn.execute("SELECT prefilter FROM accounts").fetchone()[0] == "fail"
    import_verdicts(conn, [{"id": 1, "verdict": "pass_builds", "evidence": "description",
                            "reason": "image-to-3D reconstruction is itself an ML model"}])
    row = conn.execute("SELECT prefilter, prefilter_rule, ai_depth FROM accounts").fetchone()
    assert (row["prefilter"], row["prefilter_rule"], row["ai_depth"]) == (
        "pass_builds", RULESET_LLM, "builds")


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
    assert conn.execute(
        "SELECT prefilter FROM accounts WHERE name='Eps'").fetchone()[0] == "pass_applies"


def test_export_batch_covers_everything_the_classifier_has_not_seen(conn):
    """keywords_v1 verdicts are all provisional, including its passes."""
    seed(conn, [("A", "We train models"), ("B", "Sourdough"), ("C", None)])
    apply(conn)
    batch = export_batch(conn, fund="testfund")
    assert {b["name"] for b in batch} == {"A", "B", "C"}
    assert all({"id", "homepage", "blurb", "homepage_status"} <= set(b) for b in batch)


def test_export_batch_carries_the_company_s_own_words(conn):
    """The homepage beats the investor's blurb, which is written to position a
    portfolio and routinely omits what the company is built out of."""
    seed(conn, [("A", "Game-ready on-demand 3D assets")])
    conn.execute("UPDATE accounts SET homepage_text = ?, homepage_fetch_status = 'ok'",
                 ("AI-powered 3D asset creation service",))
    batch = export_batch(conn, fund="testfund")
    assert batch[0]["homepage"].startswith("AI-powered")
    assert batch[0]["blurb"] == "Game-ready on-demand 3D assets"
    assert batch[0]["homepage_status"] == "ok"


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
        import_verdicts(conn, [{"id": 1, "verdict": "pass_builds"}])


def test_import_rejects_a_missing_id(conn):
    with pytest.raises(ValueError, match="integer id"):
        import_verdicts(conn, [{"verdict": "fail"}])


def test_excluded_accounts_are_not_judged(conn):
    seed(conn, [("A", "We train models")])
    conn.execute("UPDATE accounts SET status='excluded'")
    assert sum(apply(conn).values()) == 0


def test_summary_reports_the_pass_share():
    out = summary({"pass_builds": 1, "pass_applies": 1, "fail": 6, "unknown": 2})
    assert "20%" in out and "10 accounts" in out


def test_pass_requires_a_declared_evidence_source(conn):
    seed(conn, [("A", "Sourdough")])
    apply(conn)
    with pytest.raises(ValueError, match="evidence to be one of"):
        import_verdicts(conn, [{"id": 1, "verdict": "pass_builds", "reason": "they do ML"}])


def test_search_grounded_pass_must_cite_where_it_looked(conn):
    """Three of sixteen searches in one hand-checked sample returned a different
    company with the same name."""
    seed(conn, [("Mosaic", "Construction technology")])
    apply(conn)
    with pytest.raises(ValueError, match="must cite a URL or the company"):
        import_verdicts(conn, [{"id": 1, "verdict": "pass_builds", "evidence": "search",
                                "reason": "they have a data science team"}])
    import_verdicts(conn, [{"id": 1, "verdict": "pass_builds", "evidence": "search",
                            "reason": "mosaic.us careers page lists ML engineers"}])
    assert conn.execute("SELECT prefilter FROM accounts").fetchone()[0] == "pass_builds"


def test_description_grounded_pass_needs_no_url(conn):
    seed(conn, [("A", "We train foundation models")])
    apply(conn)
    import_verdicts(conn, [{"id": 1, "verdict": "pass_builds", "evidence": "description",
                            "reason": "says it trains foundation models"}])
    assert conn.execute("SELECT ai_depth FROM accounts").fetchone()[0] == "builds"


def test_depth_routes_the_campaign(conn):
    """A company that trains its own models and one that ships features on
    somebody else's need different copy, so depth routes a campaign."""
    routes = {"builds": "startup", "applies": "applied-ai"}
    seed(conn, [("A", "Payroll with AI features"), ("B", "We train our own models")])
    conn.execute("UPDATE accounts SET fund='testfund'")
    apply(conn)
    import_verdicts(conn, [
        {"id": 1, "verdict": "pass_applies", "evidence": "description",
         "reason": "ships AI features on third-party models"},
        {"id": 2, "verdict": "pass_builds", "evidence": "description",
         "reason": "trains its own models"},
    ], depth_routes=routes)
    rows = {r["name"]: r for r in conn.execute("SELECT name, ai_depth, tier, campaign FROM accounts")}
    assert (rows["A"]["ai_depth"], rows["A"]["campaign"]) == ("applies", "applied-ai")
    assert (rows["B"]["ai_depth"], rows["B"]["campaign"]) == ("builds", "startup")
    assert rows["A"]["tier"] == "startup"


def test_depth_route_is_config_driven_not_hardcoded(conn):
    seed(conn, [("A", "AI features")])
    conn.execute("UPDATE accounts SET fund='testfund'")
    apply(conn)
    import_verdicts(conn, [{"id": 1, "verdict": "pass_applies", "evidence": "description",
                            "reason": "applies models"}],
                    depth_routes={"applies": "some-other-campaign"})
    assert conn.execute("SELECT campaign FROM accounts").fetchone()[0] == "some-other-campaign"
