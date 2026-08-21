"""Collision handling for a group sharing one target list.

Two emails from one club to one person in a week reads as disorganised and
wastes the contact. The mechanism has to survive five people who will not read
a convention document, so it is one append-only CSV and the common path needs
no new habit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts import claims as C


@pytest.fixture
def shared(tmp_path):
    return tmp_path / "claims.csv"


def test_appending_never_rewrites_the_file(shared):
    """Two people claiming different companies must produce two new lines that
    git merges without a conflict. Rewriting or sorting the file would turn
    every concurrent edit into one."""
    C.add(shared, C.COMPANY, "Baseten", who="ada")
    first = shared.read_text()
    C.add(shared, C.COMPANY, "Modal Labs", who="bob")
    after = shared.read_text()
    assert after.startswith(first), "an existing line was rewritten"
    assert after.count("\n") == first.count("\n") + 1


def test_a_company_claimed_by_someone_else_warns(shared):
    C.add(shared, C.COMPANY, "Baseten", who="ada")
    held = C.held_by_others(shared, C.COMPANY, "baseten", me="bob")
    assert held and held[0].who == "ada"
    assert "already claimed by ada" in C.warn_line(held, C.COMPANY, "Baseten", 28)


def test_your_own_claim_is_not_a_collision(shared):
    C.add(shared, C.COMPANY, "Baseten", who="ada")
    assert C.held_by_others(shared, C.COMPANY, "Baseten", me="ada") == []


def test_person_collisions_are_caught_across_companies(shared):
    """Two members can reach the same person from different companies -- someone
    with two affiliations, or who moved. No company-level check sees that."""
    C.add(shared, C.PERSON, "ren@renkovic.test", who="ada", note="via Nimbus AI")
    held = C.held_by_others(shared, C.PERSON, "REN@RENKOVIC.TEST", me="bob")
    assert held and "renkovic" in held[0].value


def test_a_stale_claim_stops_reserving(shared):
    """Nobody should sit on a company they never worked."""
    C.add(shared, C.COMPANY, "Baseten", who="ada")
    old = datetime.now(timezone.utc) - timedelta(days=90)
    text = shared.read_text().replace(
        C.load(shared)[0].claimed_at, old.isoformat(timespec="seconds"))
    shared.write_text(text)

    held = C.held_by_others(shared, C.COMPANY, "Baseten", me="bob")
    assert held[0].is_stale(28)
    line = C.warn_line(held, C.COMPANY, "Baseten", 28)
    assert "stale" in line and "probably free" in line


def test_a_stale_claim_is_still_shown(shared):
    """'Ada looked at this two months ago' is useful even when it reserves
    nothing."""
    C.add(shared, C.COMPANY, "Baseten", who="ada")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    shared.write_text(shared.read_text().replace(C.load(shared)[0].claimed_at, old))
    assert "ada" in C.warn_line(
        C.held_by_others(shared, C.COMPANY, "Baseten", me="bob"), C.COMPANY, "B", 28)


def test_no_claims_file_disables_the_check_silently(shared):
    """One person running this alone should never see any of it."""
    assert C.load(None) == []
    assert C.held_by_others(None, C.COMPANY, "Baseten") == []
    assert C.warn_line([], C.COMPANY, "Baseten", 28) == ""


def test_a_missing_file_is_not_an_error(tmp_path):
    """The first person to run it has not created the file yet."""
    assert C.load(tmp_path / "never-made.csv") == []


def test_sending_records_the_claim_without_a_new_habit():
    """An explicit claim command exists, but the common path must not depend on
    remembering it -- a mechanism that needs discipline is the one that fails."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "scripts" / "outbound.py").read_text()
    marker = src.index('def mark_sent')
    body = src[marker:marker + 3000]
    assert "C.add(cfg.campaign.claims_file" in body, \
        "mark-sent must record claims itself"
    assert "C.PERSON" in body and "C.COMPANY" in body, \
        "both collision kinds must be recorded"
