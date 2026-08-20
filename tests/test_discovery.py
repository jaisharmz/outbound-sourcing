"""Company discovery, and the industry-research adapter in particular.

The fixtures mirror the real output shape rather than the shape the source
skill's documentation describes -- those have already diverged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.discover_companies import (
    DiscoveryReport,
    domain_candidate,
    from_industry_run,
    from_name_list,
    read_run_json,
    read_yaml_block,
    upsert,
    _next_status,
)

ROOT = Path(__file__).resolve().parent
RUN = ROOT / "fixtures" / "industry_run"
EMPTY_RUN = ROOT / "fixtures" / "industry_run_empty"
TIERS = {"startup", "frontier-lab"}


@pytest.fixture
def campaigns(config_root):
    """config.example ships no campaigns.yaml; add the two-campaign split."""
    (config_root / "campaigns.yaml").write_text(
        "campaigns:\n"
        "  startup:\n    tiers: [startup]\n"
        "  frontier-lab:\n    tiers: [frontier-lab]\n"
    )
    from scripts.config import Config
    return Config(config_root)


# ------------------------------------------------------------ domain candidate


def test_homepage_url_yields_a_domain():
    assert domain_candidate("https://homepagestartup.test/") == ("homepagestartup.test", "candidate")


def test_www_is_stripped():
    assert domain_candidate("https://www.nvidia.com/")[0] == "nvidia.com"


def test_arxiv_url_yields_no_domain():
    """Google DeepMind's landscape url is an arXiv abstract. Taking arxiv.org as
    its sending domain would mail a stranger at the wrong company."""
    assert domain_candidate("https://arxiv.org/abs/2501.00663") == (None, "aggregator")


@pytest.mark.parametrize("url", [
    "https://github.com/LMCache/LMCache",
    "https://someone.github.io/project",
    "https://docs.google.com/document/d/abc",
    "https://www.linkedin.com/company/x",
    "https://www.theinformation.com/articles/x",
    "https://semanticscholar.org/paper/x",
])
def test_aggregator_urls_yield_no_domain(url):
    assert domain_candidate(url)[0] is None


def test_google_itself_is_not_an_aggregator():
    """google.com is a legitimate target; docs.google.com is not evidence about it."""
    assert domain_candidate("https://cloud.google.com/tpu")[0] == "google.com"


def test_missing_or_malformed_url():
    assert domain_candidate(None) == (None, "unknown")
    assert domain_candidate("not a url") == (None, "unknown")


# ------------------------------------------------------------ parsing


def test_reads_the_fenced_yaml_block_not_the_prose():
    block = read_yaml_block(RUN / "landscape.md")
    assert block is not None
    assert {"orgs", "excluded", "inclusion_test"} <= set(block)
    assert len(block["orgs"]) == 5


def test_run_json_is_read_leniently():
    """The real shape gained keys and lost profile_hash; depend on neither."""
    meta = read_run_json(RUN)
    assert meta["slug"] == "example"
    assert "profile_hash" not in meta
    assert meta["degraded"]["websearch"]


def test_missing_run_json_is_not_fatal(tmp_path):
    assert read_run_json(tmp_path) == {}


# ------------------------------------------------------------ industry import


def test_imports_only_the_configured_tiers(campaigns):
    records, report = from_industry_run(campaigns, RUN, TIERS)
    names = {r.name for r in records if not r.excluded_reason}
    assert "Homepage Startup" in names
    assert "Arxiv Lab" in names
    assert "Personal Site Lab" not in names      # academic
    assert any("Personal Site Lab" in s for s in report.skipped_tier)


def test_tier_maps_to_a_campaign(campaigns):
    records, _ = from_industry_run(campaigns, RUN, TIERS)
    by_name = {r.name: r for r in records}
    assert by_name["Homepage Startup"].campaign == "startup"
    assert by_name["Arxiv Lab"].campaign == "frontier-lab"


def test_evidence_url_does_not_become_a_domain(campaigns):
    records, report = from_industry_run(campaigns, RUN, TIERS)
    by_name = {r.name: r for r in records}
    assert by_name["Arxiv Lab"].domain is None
    assert by_name["Docs Company"].domain is None
    assert by_name["Homepage Startup"].domain == "homepagestartup.test"
    assert "Arxiv Lab" in report.no_domain


def test_optional_keys_are_tolerated(campaigns):
    """stage/raised/investors/headcount appear in one real run out of three."""
    records, _ = from_industry_run(campaigns, RUN, TIERS)
    by_name = {r.name: r for r in records}
    assert by_name["Homepage Startup"].ships is True
    assert by_name["Docs Company"].ships is False
    assert by_name["Funded Startup"].subproblems == []


def test_excluded_companies_are_carried_with_their_reason(campaigns):
    records, _ = from_industry_run(campaigns, RUN, TIERS)
    ex = [r for r in records if r.excluded_reason]
    assert [r.name for r in ex] == ["Excluded Co"]
    assert "different thing" in ex[0].excluded_reason


def test_degraded_run_is_reported(campaigns):
    _, report = from_industry_run(campaigns, RUN, TIERS)
    assert report.degraded_run
    assert "200/200" in report.degraded_run


def test_run_without_a_landscape_block_falls_back_to_frontmatter(campaigns, tmp_path):
    import shutil
    dst = tmp_path / "run"
    shutil.copytree(RUN, dst)
    (dst / "landscape.md").write_text("# Landscape\n\nNo yaml here.\n")
    records, report = from_industry_run(campaigns, dst, TIERS)
    assert {r.name for r in records} == {"Frontmatter Only Co", "Homepage Startup"}
    assert all(r.domain is None for r in records)
    assert any("key_companies" in w for w in report.warnings)


def test_incomplete_run_imports_nothing_and_says_so(campaigns):
    """A real run on disk has an empty avenues/ dir and no landscape block."""
    records, report = from_industry_run(campaigns, EMPTY_RUN, TIERS)
    assert records == []
    assert len(report.warnings) == 2


def test_missing_directory_is_an_error(campaigns, tmp_path):
    from scripts.errors import ConfigError
    with pytest.raises(ConfigError, match="not a directory"):
        from_industry_run(campaigns, tmp_path / "nope", TIERS)


# ------------------------------------------------------------ persistence


def test_upsert_writes_accounts(conn, campaigns):
    records, report = from_industry_run(campaigns, RUN, TIERS)
    upsert(conn, records, report, degraded=True)
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM accounts")}
    assert rows["Homepage Startup"]["campaign"] == "startup"
    assert rows["Homepage Startup"]["status"] == "degraded"
    assert rows["Excluded Co"]["status"] == "excluded"
    assert rows["Arxiv Lab"]["domain"] is None
    assert rows["Arxiv Lab"]["domain_confidence"] == "aggregator"


def test_reimport_is_idempotent(conn, campaigns):
    records, report = from_industry_run(campaigns, RUN, TIERS)
    upsert(conn, records, report)
    n1 = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    upsert(conn, records, DiscoveryReport(), degraded=False)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == n1


def test_exclusion_is_sticky(conn, campaigns):
    """Someone decided against this company. A later run listing it must not
    quietly put it back in the queue."""
    records, report = from_industry_run(campaigns, RUN, TIERS)
    upsert(conn, records, report)
    from scripts.discover_companies import CompanyRecord
    upsert(conn, [CompanyRecord(name="Excluded Co", tier="startup", campaign="startup",
                                domain="excludedco.test", source="list")], DiscoveryReport())
    assert conn.execute(
        "SELECT status FROM accounts WHERE name = 'Excluded Co'"
    ).fetchone()["status"] == "excluded"


def test_research_progress_is_not_demoted(conn, campaigns):
    records, report = from_industry_run(campaigns, RUN, TIERS)
    upsert(conn, records, report)
    conn.execute("UPDATE accounts SET status = 'done' WHERE name = 'Homepage Startup'")
    upsert(conn, records, DiscoveryReport(), degraded=True)
    assert conn.execute(
        "SELECT status FROM accounts WHERE name = 'Homepage Startup'"
    ).fetchone()["status"] == "done"


def test_next_status_rules():
    assert _next_status(None, False, False) == "new"
    assert _next_status(None, False, True) == "degraded"
    assert _next_status(None, True, False) == "excluded"
    assert _next_status("excluded", False, True) == "excluded"
    assert _next_status("done", False, True) == "done"
    assert _next_status("researching", False, False) == "researching"
    assert _next_status("degraded", False, False) == "new"


def test_a_known_domain_is_not_overwritten(conn, campaigns):
    from scripts.discover_companies import CompanyRecord
    upsert(conn, [CompanyRecord(name="Homepage Startup", domain="known.test",
                                domain_confidence="declared", source="list")],
           DiscoveryReport())
    records, report = from_industry_run(campaigns, RUN, TIERS)
    upsert(conn, records, report)
    row = conn.execute("SELECT domain, domain_confidence FROM accounts"
                       " WHERE name = 'Homepage Startup'").fetchone()
    assert row["domain"] == "known.test"
    assert row["domain_confidence"] == "declared"


# ------------------------------------------------------------ list mode


def test_name_list_with_and_without_domains(tmp_path, campaigns):
    path = tmp_path / "companies.txt"
    path.write_text("# a comment\nAlpha Co,alpha.test\nBeta Co\n\n")
    records, report = from_name_list(path, "list", campaigns, tier="startup")
    assert [r.name for r in records] == ["Alpha Co", "Beta Co"]
    assert records[0].domain == "alpha.test"
    assert records[0].domain_confidence == "declared"
    assert records[1].domain is None
    assert records[0].campaign == "startup"


def test_missing_list_file_is_an_error(tmp_path, campaigns):
    from scripts.errors import ConfigError
    with pytest.raises(ConfigError, match="not found"):
        from_name_list(tmp_path / "nope.txt", "list", campaigns)


# ------------------------------------------------------------ source chain

BROKEN = ROOT / "fixtures" / "industry_run_broken"
REPORT = ROOT / "fixtures" / "industry_run_report"


def test_report_json_is_preferred_over_the_markdown_block(campaigns):
    """report.json carries the same structures as real JSON, so it cannot be
    lost to a YAML quoting mistake in prose."""
    records, report = from_industry_run(campaigns, REPORT, TIERS)
    names = {r.name for r in records}
    assert "Json Startup" in names
    assert "Should Not Be Used" not in names
    assert any("report.json" in w for w in report.warnings)


def test_report_json_optional_keys(campaigns):
    records, _ = from_industry_run(campaigns, REPORT, TIERS)
    by_name = {r.name: r for r in records}
    assert by_name["Json Startup"].domain == "jsonstartup.test"
    assert by_name["Json Lab"].domain is None          # arXiv evidence url
    assert by_name["Json Excluded"].excluded_reason == "Off topic."


def test_one_malformed_record_does_not_cost_the_whole_block():
    """A single bad record otherwise takes out an entire run: one real file has
    `what: "Critique of World Model," at v5 ...` and lost ~30 companies to it."""
    from scripts.discover_companies import DiscoveryReport as R
    rep = R()
    block = read_yaml_block(BROKEN / "landscape.md", rep)
    names = {o["name"] for o in block["orgs"]}
    assert names == {"Good One", "Good Two"}
    assert "Broken Record" not in names
    assert any("dropped 1" in w for w in rep.warnings)


def test_salvage_still_recovers_the_excluded_list():
    from scripts.discover_companies import DiscoveryReport as R
    block = read_yaml_block(BROKEN / "landscape.md", R())
    assert [e["name"] for e in block["excluded"]] == ["Excluded One"]


def test_salvage_reports_what_it_dropped(campaigns):
    """Silent truncation reads as coverage. It has to be said out loud."""
    _, report = from_industry_run(campaigns, BROKEN, TIERS)
    assert any("YAML syntax error" in w and "dropped" in w for w in report.warnings)


def test_frontmatter_fallback_marks_records_as_needing_a_tier(campaigns, tmp_path):
    import shutil
    dst = tmp_path / "run"
    shutil.copytree(RUN, dst)
    (dst / "landscape.md").write_text("# Landscape\n")
    _, report = from_industry_run(campaigns, dst, TIERS)
    assert report.no_tier
    assert "cannot enroll" in report.summary()
