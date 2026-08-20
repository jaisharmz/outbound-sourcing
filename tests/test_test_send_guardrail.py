"""--to is the only path that reaches an arbitrary address without passing the
review gate or the suppression list. These tests are the guardrail on it."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from scripts.config import Config
from scripts.outbound import app
from scripts.suppression import add

runner = CliRunner()


@pytest.fixture
def cfg_with_allowlist(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + '\ntest_send_allowlist:\n'
                                       '  - teammate@example.com\n'
                                       '  - "*@mail-tester.com"\n')
    return config_root


def run(cfg_root, db, *args):
    return runner.invoke(app, ["test-email", "--mailbox", "console", "--config", str(cfg_root),
                               "--db", str(db), "--wait", "0", *args])


def test_test_recipient_is_always_allowed(cfg_with_allowlist, tmp_path):
    res = run(cfg_with_allowlist, tmp_path / "a.db")
    assert res.exit_code == 0
    assert "REFUSED" not in res.output


def test_allowlisted_address_is_allowed(cfg_with_allowlist, tmp_path):
    res = run(cfg_with_allowlist, tmp_path / "b.db", "--to", "teammate@example.com")
    assert res.exit_code == 0
    assert "REFUSED" not in res.output


def test_wildcard_matches_the_domain(cfg_with_allowlist, tmp_path):
    res = run(cfg_with_allowlist, tmp_path / "c.db", "--to", "abc123@mail-tester.com")
    assert res.exit_code == 0


def test_wildcard_matches_a_subdomain(cfg_with_allowlist, tmp_path):
    """mail-tester hands out addresses at srv*.mail-tester.com."""
    res = run(cfg_with_allowlist, tmp_path / "d.db", "--to", "abc123@srv1.mail-tester.com")
    assert res.exit_code == 0


def test_unlisted_address_is_refused(cfg_with_allowlist, tmp_path):
    res = run(cfg_with_allowlist, tmp_path / "e.db", "--to", "stranger@elsewhere.test")
    assert res.exit_code == 1
    assert "REFUSED" in res.output
    assert "test_send_allowlist" in res.output


def test_force_allows_an_unlisted_address_with_a_loud_warning(cfg_with_allowlist, tmp_path):
    res = run(cfg_with_allowlist, tmp_path / "f.db", "--to", "stranger@elsewhere.test", "--force")
    assert res.exit_code == 0
    assert "FORCED TEST SEND TO AN UNLISTED ADDRESS" in res.output
    assert "has not been through the" in res.output


def test_suppressed_address_is_refused_even_allowlisted(cfg_with_allowlist, tmp_path):
    from scripts.db import open_db
    db = tmp_path / "g.db"
    conn = open_db(db)
    add(conn, "email", "teammate@example.com", "unsubscribed")
    res = run(cfg_with_allowlist, db, "--to", "teammate@example.com")
    assert res.exit_code == 1
    assert "suppression list" in res.output


def test_force_does_not_override_suppression(cfg_with_allowlist, tmp_path):
    """An opt-out is permanent and global. "It was only a test" is not an
    exception the recipient agreed to."""
    from scripts.db import open_db
    db = tmp_path / "h.db"
    conn = open_db(db)
    add(conn, "email", "gone@elsewhere.test", "unsubscribed")
    res = run(cfg_with_allowlist, db, "--to", "gone@elsewhere.test", "--force")
    assert res.exit_code == 1
    assert "--force does not override it" in res.output


def test_suppressed_domain_is_refused(cfg_with_allowlist, tmp_path):
    from scripts.db import open_db
    db = tmp_path / "i.db"
    conn = open_db(db)
    add(conn, "domain", "elsewhere.test", "bounced")
    res = run(cfg_with_allowlist, db, "--to", "anyone@elsewhere.test", "--force")
    assert res.exit_code == 1


def test_every_outcome_is_recorded_in_test_sends(cfg_with_allowlist, tmp_path):
    from scripts.db import open_db
    db = tmp_path / "j.db"
    run(cfg_with_allowlist, db, "--to", "teammate@example.com")
    run(cfg_with_allowlist, db, "--to", "stranger@elsewhere.test")
    run(cfg_with_allowlist, db, "--to", "stranger@elsewhere.test", "--force")

    conn = open_db(db)
    rows = conn.execute(
        "SELECT to_addr, ok, allowlisted, forced, error FROM test_sends ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert (rows[0]["to_addr"], rows[0]["ok"], rows[0]["allowlisted"]) == \
           ("teammate@example.com", 1, 1)
    assert rows[1]["ok"] == 0 and "allowlist" in rows[1]["error"]
    assert (rows[2]["ok"], rows[2]["forced"]) == (1, 1)


def test_allowlist_rejects_a_malformed_entry(config_root):
    from scripts.errors import ConfigError
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + '\ntest_send_allowlist:\n  - "not an address"\n')
    with pytest.raises(ConfigError, match="neither an email address nor"):
        Config(config_root)


def test_bare_wildcard_is_rejected(config_root):
    from scripts.errors import ConfigError
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + '\ntest_send_allowlist:\n  - "*@localhost"\n')
    with pytest.raises(ConfigError, match="real domain"):
        Config(config_root)
