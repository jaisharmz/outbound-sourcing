"""The production database must be unreachable from anything that is not the
real thing. `demo` defaulting to it seeded fixture companies into real data and
shifted every account id, and that was found by accident."""

from __future__ import annotations

import pytest

from scripts.db import ProductionDatabaseError, connect, default_db_path


def test_production_database_is_refused_under_pytest():
    with pytest.raises(ProductionDatabaseError, match="refusing to open"):
        connect(default_db_path())


def test_the_default_path_is_also_refused():
    """connect() with no argument resolves to production."""
    with pytest.raises(ProductionDatabaseError):
        connect()


def test_the_error_says_what_to_do_instead():
    with pytest.raises(ProductionDatabaseError) as exc:
        connect()
    assert "scratch database" in str(exc.value)


def test_a_scratch_database_opens_normally(tmp_path):
    conn = connect(tmp_path / "scratch.db")
    assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_guard_is_keyed_on_the_resolved_path(tmp_path, monkeypatch):
    """A relative path pointing at production is still production."""
    import os
    monkeypatch.chdir(default_db_path().parent)
    with pytest.raises(ProductionDatabaseError):
        connect(default_db_path().name)
