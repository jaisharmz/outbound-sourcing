"""Loading a company list into accounts.

What survives after `--mode vc` and `--mode industry` were removed: the list
importer, the account upsert, and the status rules that stop a re-import from
demoting a company whose research already progressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.discover_companies import (
    CompanyRecord,
    DiscoveryReport,
    _next_status,
    from_name_list,
    upsert,
)
from scripts.errors import ConfigError


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


# ---------------------------------------------------------------- upsert
#
# These build records directly rather than through an importer. The code under
# test is upsert, and routing them through an adapter only coupled the tests to
# whichever importer happened to exist.


def _records(campaigns):
    return [
        CompanyRecord(name="Homepage Startup", domain="homepagestartup.test",
                      domain_confidence="declared", tier="startup",
                      campaign="startup", source="list"),
        CompanyRecord(name="Excluded Co", domain=None, tier="startup",
                      campaign="startup", source="list",
                      source_note="the run said this is a different thing"),
        CompanyRecord(name="Arxiv Lab", domain=None,
                      domain_confidence="aggregator", tier="startup",
                      campaign="startup", source="list"),
    ]


def test_upsert_writes_accounts(conn, campaigns):
    upsert(conn, _records(campaigns), DiscoveryReport(), degraded=True)
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM accounts")}
    assert rows["Homepage Startup"]["campaign"] == "startup"
    assert rows["Homepage Startup"]["status"] == "degraded"
    # Not excluded: it carries the source note and re-queues like any other
    # account from a degraded run.
    assert rows["Excluded Co"]["status"] == "degraded"
    assert rows["Excluded Co"]["excluded_reason"] is None
    assert "different thing" in rows["Excluded Co"]["source_note"]
    assert rows["Arxiv Lab"]["domain"] is None
    assert rows["Arxiv Lab"]["domain_confidence"] == "aggregator"


def test_reimport_is_idempotent(conn, campaigns):
    records = _records(campaigns)
    upsert(conn, records, DiscoveryReport())
    n1 = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    upsert(conn, records, DiscoveryReport(), degraded=False)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == n1


def test_research_progress_is_not_demoted(conn, campaigns):
    records = _records(campaigns)
    upsert(conn, records, DiscoveryReport())
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
    upsert(conn, [CompanyRecord(name="Homepage Startup", domain="known.test",
                                domain_confidence="declared", source="list")],
           DiscoveryReport())
    upsert(conn, _records(campaigns), DiscoveryReport())
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

