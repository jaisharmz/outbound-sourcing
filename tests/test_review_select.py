"""Previews must span the ways an email differs, not repeat the happy path."""

from __future__ import annotations

from scripts.db import utcnow
from scripts.review_select import choose


def add(conn, name, *, basis="observed", pz=None, verification="valid"):
    conn.execute(
        "INSERT INTO accounts (name, name_normalized, source, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)", (name, name.lower(), "list", "new", utcnow(), utcnow()))
    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO contacts (account_id, name, first_name, title, email, email_domain,"
        " email_basis, confidence, personalization, personalization_source_url,"
        " verification_status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, name, name.split()[0], "Research Scientist", f"{name.lower().replace(' ','')}@x.test",
         "x.test", basis, 0.9, pz, "https://x.test/p" if pz else None,
         verification, utcnow(), utcnow()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_previews_span_the_axes_rather_than_taking_the_first_five(conn):
    """The first five by id are all observed+personalized here, which is exactly
    the happy path that hid a blank-line bug for three milestones."""
    for i in range(5):
        add(conn, f"Happy {i}", basis="observed", pz="A sourced sentence.")
    add(conn, "Null Person", basis="observed", pz=None)
    add(conn, "Guessed Addr", basis="inferred_from_pattern", pz="A sourced sentence.")
    add(conn, "Catchall Co", basis="observed", pz="A sourced sentence.",
        verification="catch_all")

    picked = choose(conn)
    reasons = " | ".join(p.reason for p in picked)
    assert len(picked) == 5
    assert "falls back" in reasons
    assert "inferred from a pattern" in reasons
    assert "accept-all" in reasons


def test_null_personalization_is_picked_first(conn):
    """It is the common case and the branch a real bug shipped in."""
    for i in range(3):
        add(conn, f"Happy {i}", pz="A sourced sentence.")
    add(conn, "Null Person", pz=None)
    assert "falls back" in choose(conn)[0].reason


def test_no_contact_is_previewed_twice(conn):
    add(conn, "Only One", basis="inferred_from_pattern", pz=None)
    picked = choose(conn)
    assert len({p.contact_id for p in picked}) == len(picked)


def test_fills_from_what_is_left_when_axes_run_out(conn):
    for i in range(7):
        add(conn, f"Same {i}", basis="observed", pz="A sourced sentence.")
    picked = choose(conn)
    assert len(picked) == 5
    assert any("filler" in p.reason for p in picked)


def test_approved_rows_are_not_previewed(conn):
    add(conn, "Already Done", pz=None)
    conn.execute("UPDATE contacts SET approved = 1")
    assert choose(conn) == []


def test_review_columns_expose_the_basis_and_its_sample_count():
    """`first.last` at confidence 1.00 from three addresses and from one are very
    different claims, and the reviewer is the only check on either."""
    from scripts.review_select import REVIEW_COLUMNS
    for col in ("email_basis", "email_pattern", "email_pattern_samples",
                "email_pattern_confidence", "verification_status"):
        assert col in REVIEW_COLUMNS


def test_risk_flags_name_what_could_be_wrong(conn):
    """The gate is the only check between an inferred address and a stranger."""
    from scripts.review import risk_flags

    class R(dict):
        def __getitem__(self, k): return self.get(k)

    single = R(email_basis="inferred_from_pattern", email_pattern_samples=1,
               email_pattern_confidence=1.0, verification_status="valid",
               personalization="x", liveness_status="live", observed_at=None)
    flags = " | ".join(risk_flags(single))
    assert "INFERRED" in flags and "single-sample" in flags

    mixed = R(email_basis="inferred_from_pattern", email_pattern_samples=8,
              email_pattern_confidence=0.5, verification_status="valid",
              personalization="x", liveness_status="live", observed_at=None)
    assert "close to a guess" in " | ".join(risk_flags(mixed))

    stale = R(email_basis="observed", email_pattern_samples=3,
              email_pattern_confidence=0.9, verification_status="valid",
              personalization="x", liveness_status="live",
              observed_at="2021-01-01T00:00:00+00:00")
    assert "years ago" in " | ".join(risk_flags(stale))

    clean = R(email_basis="observed", email_pattern_samples=3,
              email_pattern_confidence=0.9, verification_status="valid",
              personalization="x", liveness_status="live", observed_at=None)
    assert risk_flags(clean) == []
