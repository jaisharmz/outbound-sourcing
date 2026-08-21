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


# --------------------------------------------------- default-deny (2nd occurrence)


def test_an_inline_python_script_cannot_open_production(monkeypatch, tmp_path):
    """The guard used to be opt-in, firing only under pytest or an env var, so
    an ad-hoc `python -c` -- what people actually reach for while checking
    something -- wrote a junk row into real data. Same shape as demo defaulting
    to production. Twice is a pattern, so the rule is now default-deny."""
    import sys
    import types

    from scripts import db

    prod = tmp_path / "prospects.db"
    monkeypatch.setattr(db, "default_db_path", lambda: prod)
    monkeypatch.delenv("OUTBOUND_ALLOW_PROD", raising=False)
    monkeypatch.delenv("OUTBOUND_NO_PROD_DB", raising=False)
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))

    with pytest.raises(db.ProductionDatabaseError) as exc:
        db.connect(prod)
    assert "python -c" in str(exc.value)
    assert "OUTBOUND_DB" in str(exc.value)


def test_the_cli_entrypoint_is_allowed(monkeypatch, tmp_path):
    import sys
    import types

    from scripts import db

    prod = tmp_path / "prospects.db"
    monkeypatch.setattr(db, "default_db_path", lambda: prod)
    monkeypatch.delenv("OUTBOUND_NO_PROD_DB", raising=False)
    monkeypatch.setattr(sys, "argv", ["/some/venv/bin/outbound", "drafts"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))
    db.connect(prod).close()          # must not raise


def test_explicit_opt_in_still_works(monkeypatch, tmp_path):
    """An escape hatch that has to be typed is fine; one that is the default is not."""
    import sys
    import types

    from scripts import db

    prod = tmp_path / "prospects.db"
    monkeypatch.setattr(db, "default_db_path", lambda: prod)
    monkeypatch.delenv("OUTBOUND_NO_PROD_DB", raising=False)
    monkeypatch.setenv("OUTBOUND_ALLOW_PROD", "1")
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))
    db.connect(prod).close()          # must not raise
