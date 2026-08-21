"""Transitive personal exclusions.

The failure that matters is under-exclusion: cold-emailing someone the operator
sees at group meeting costs the relationship. So these tests pin the hop rules
in both directions -- what must be excluded, and what must not be, because an
exclusion that spreads two hops would quietly eat most of the field.
"""

from __future__ import annotations

import datetime

import pytest

from scripts import exclusions, graph
from scripts.config import Config

URL = "https://example.edu/lab/people"
YEAR = datetime.date.today().year


@pytest.fixture
def excl_config(config_root):
    (config_root / "personal_exclusions.yaml").write_text(
        "labs:\n"
        "  - name: Dawn Song\n"
        "    institution: UC Berkeley\n"
        "    reason: The operator is a member of this lab.\n"
        "    aliases: [SecML]\n"
        "people: []\n"
        "companies: []\n"
    )
    return Config(config_root)


def _person(conn, name):
    return graph.upsert_node(conn, "person", name)


def test_the_named_person_is_excluded(conn, excl_config):
    _person(conn, "Dawn Song")
    ex = exclusions.refresh(conn, excl_config)
    assert [e["name"] for e in ex.values()] == ["Dawn Song"]
    assert exclusions.check(conn, excl_config, "Dawn Song")


def test_a_student_is_excluded_through_the_advising_edge(conn, excl_config):
    seed, student = _person(conn, "Dawn Song"), _person(conn, "Former Student")
    graph.add_edge(conn, student, seed, "advised_by", source_url=URL, year=2015)
    ex = exclusions.refresh(conn, excl_config)
    hit = exclusions.check(conn, excl_config, "Former Student")
    assert hit and hit["hops"] == 1
    assert "advised by Dawn Song" in hit["reason"]
    assert hit["source_url"] == URL     # every exclusion carries its evidence


def test_advising_has_no_recency_gate(conn, excl_config):
    """An advising relationship does not expire. A student from 2009 is still
    someone the operator would be introduced to, not cold-emailed."""
    seed, student = _person(conn, "Dawn Song"), _person(conn, "Old Student")
    graph.add_edge(conn, student, seed, "advised_by", source_url=URL, year=2009)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Old Student")


def test_a_current_collaborator_is_excluded(conn, excl_config):
    seed, collab = _person(conn, "Dawn Song"), _person(conn, "Recent Coauthor")
    graph.add_edge(conn, seed, collab, "coauthored_with", source_url=URL, year=YEAR - 1)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Recent Coauthor")


def test_a_stale_coauthor_is_not_excluded(conn, excl_config):
    """'Current collaborators' was the instruction. A paper from a decade ago is
    a fact about the past that both parties may have forgotten."""
    seed, old = _person(conn, "Dawn Song"), _person(conn, "Ancient Coauthor")
    graph.add_edge(conn, seed, old, "coauthored_with", source_url=URL,
                   year=YEAR - exclusions.COLLABORATION_RECENCY_YEARS - 1)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Ancient Coauthor") is None


def test_a_coworker_is_not_excluded(conn, excl_config):
    """A shared employer is not a personal tie -- that is what company-level
    suppression is for. Otherwise excluding one person at Google excludes Google."""
    seed = _person(conn, "Dawn Song")
    org = graph.upsert_node(conn, "organization", "UC Berkeley")
    other = _person(conn, "Unrelated Colleague")
    graph.add_edge(conn, seed, org, "works_at", source_url=URL, is_current=True)
    graph.add_edge(conn, other, org, "works_at", source_url=URL, is_current=True)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Unrelated Colleague") is None


def test_exclusion_does_not_travel_two_hops(conn, excl_config):
    """The rule the operator set. Two hops from a well-connected professor is
    most of the field, and a student's coauthor is not someone they know."""
    seed, student = _person(conn, "Dawn Song"), _person(conn, "Student")
    stranger = _person(conn, "Student's Coauthor")
    graph.add_edge(conn, student, seed, "advised_by", source_url=URL, year=YEAR - 2)
    graph.add_edge(conn, student, stranger, "coauthored_with", source_url=URL, year=YEAR)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Student")
    assert exclusions.check(conn, excl_config, "Student's Coauthor") is None


def test_lab_membership_excludes(conn, excl_config):
    lab = graph.upsert_node(conn, "lab", "Dawn Song's group")
    member = _person(conn, "Lab Member")
    graph.add_edge(conn, member, lab, "lab_member_of", source_url=URL)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Lab Member")


def test_an_alias_matches_the_lab(conn, excl_config):
    assert excl_config.personal_exclusions.excluded_lab("the SecML group")


def test_exclusion_is_recomputed_not_accumulated(conn, excl_config):
    """The graph grows, so yesterday's answer is not today's. A stale row would
    keep excluding someone after the edge that justified it was corrected."""
    seed, student = _person(conn, "Dawn Song"), _person(conn, "Student")
    graph.add_edge(conn, student, seed, "advised_by", source_url=URL, year=YEAR)
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Student")

    conn.execute("DELETE FROM graph_edges")
    exclusions.refresh(conn, excl_config)
    assert exclusions.check(conn, excl_config, "Student") is None


def test_check_catches_a_lab_before_the_graph_knows_the_person(conn, excl_config):
    """Ingest sees candidates that traversal may never have made a node for."""
    hit = exclusions.check(conn, excl_config, "Never Seen", lab="Dawn Song's lab")
    assert hit and "excluded lab" in hit["reason"]


# ----------------------------------------------------------------- lab suppression


def test_lab_suppression_actually_blocks(conn):
    """suppress_lab wrote rows that is_suppressed never read, so lab suppression
    looked implemented while every send went out anyway."""
    from scripts.suppression import is_suppressed, suppress_lab

    suppress_lab(conn, "Dawn Song's group", "replied asking to stop")
    assert is_suppressed(conn, "x@university.test", lab="Dawn Song's group")
    assert is_suppressed(conn, "x@university.test", lab="Some Other Lab") is None


def test_per_lab_cap_counts_contacts(conn):
    """Four people from one group receive what reads as a campaign against it."""
    from scripts.suppression import lab_is_full

    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at)"
                 " VALUES (1, 'Some University', 'some university', 'test', 'active', '', '')")
    for i in range(2):
        conn.execute("INSERT INTO contacts (account_id, name, first_name, last_name,"
                     " title, email, email_domain, email_basis, confidence, lab, sendable,"
                     " created_at, updated_at)"
                     " VALUES (1,?,?,'X','Researcher',?, 'x.test','observed',0.9,"
                     " 'Some Lab',1,'','')",
                     (f"P{i}", f"P{i}", f"p{i}@x.test"))
    assert lab_is_full(conn, "Some Lab", 2)
    assert not lab_is_full(conn, "Some Lab", 3)
    assert not lab_is_full(conn, "Other Lab", 2)


# ------------------------------------------------------------ review flags


def test_a_personal_domain_is_not_flagged_as_stale(conn):
    """ren@renkovic.test is more durable than an @nimbus.test address, not less.
    Flagging it trains the reviewer to dismiss the flag that catches the real
    case -- an address left behind at a previous employer."""
    from scripts.review import risk_flags

    def row(**kw):
        base = {"email_basis": "observed", "email_pattern_samples": 0,
                "email_pattern_confidence": 0, "observed_at": None,
                "verification_status": "mx_only", "personalization": "x",
                "personalization_source_url": "https://x.test", "liveness_status": None,
                "name": "Ren Kovic", "email": "ren@renkovic.test",
                "title": "Research Scientist", "account_domain": "nimbus.test"}
        base.update(kw)
        return base

    assert not any("predate" in f for f in risk_flags(row()))
    stale = risk_flags(row(name="Shang Zhu", email="szhu@state-university.test"))
    assert any("state-university.test" in f and "nimbus.test" in f for f in stale)


def test_mx_only_is_not_a_risk_flag(conn):
    """Port 25 is blocked here, so mx_only is the ceiling. A flag on 100% of
    rows carries no information and buries the ones that do."""
    from scripts.review import risk_flags

    flags = risk_flags({"email_basis": "observed", "email_pattern_samples": 0,
                        "email_pattern_confidence": 0, "observed_at": None,
                        "verification_status": "mx_only", "personalization": "x",
                        "personalization_source_url": "https://x.test",
                        "liveness_status": None, "name": "A B",
                        "title": "Research Scientist",
                        "email": "a@b.test", "account_domain": "b.test"})
    assert not any("mx_only" in f or "port 25" in f for f in flags)


# ------------------------------------------------------- person page probing


def test_an_uncorroborated_page_is_a_namesake_not_a_contact(monkeypatch):
    """Probing "Pankaj Gupta" for Baseten found a real page belonging to a
    different Pankaj Gupta and read a stranger's address off it. Both name
    tokens were present, so every check passed and the hit looked clean."""
    from scripts import person_pages
    from scripts.homepages import HomepageResult

    page = ('<html><body>Pankaj Gupta. Yoga instructor. '
            '<a href="mailto:someone@unrelated-hobby.test">mail</a></body></html>')
    monkeypatch.setattr(person_pages, "fetch_one",
                        lambda url, timeout=20: HomepageResult(
                            url, "ok", text="Pankaj Gupta. Yoga instructor.", raw=page))

    hit = person_pages.find("Pankaj Gupta", company="Baseten")
    assert hit.status == "namesake_risk"
    assert not hit.corroborated
    assert hit.emails == ["someone@unrelated-hobby.test"]      # surfaced, never promoted

    # Same page, no company asked for: nothing to corroborate against.
    assert person_pages.find("Pankaj Gupta").status == "found"


def test_a_corroborated_page_is_a_real_hit(monkeypatch):
    from scripts import person_pages
    from scripts.homepages import HomepageResult

    page = ('<html><body>Jisen Li, AI Researcher at Nimbus AI. '
            'jlee@nimbus.test</body></html>')
    monkeypatch.setattr(person_pages, "fetch_one",
                        lambda url, timeout=20: HomepageResult(
                            url, "ok", text="Jisen Li, AI Researcher at Nimbus AI. "
                                            "jlee@nimbus.test", raw=page))
    hit = person_pages.find("Jisen Li", company="Nimbus AI")
    assert hit.status == "found" and hit.corroborated
    assert "jlee@nimbus.test" in hit.emails


# ------------------------------------------------------ leadership at ingest


def test_a_founder_is_dropped_at_ingest(conn, config, candidates_dir, monkeypatch):
    """The filter was written and then never called -- it only ever ran from an
    ad-hoc script, so a founder would have reached the queue in a real run. It
    now runs once per company file, covering every candidate in it."""
    from scripts import ingest_candidates, leadership
    from scripts.ingest_candidates import ingest

    seen = {}

    def fake_scan(domain, names, timeout=15):
        seen["names"] = list(names)
        return {names[0]: f"https://{domain}/about -- Alan Turing Co-Founder"}

    monkeypatch.setattr(ingest_candidates.leadership, "scan", fake_scan)
    report = ingest(conn, config, candidates_dir)
    assert seen.get("names"), "the scan was never called"
    assert report.dropped_leadership, "a listed founder was not dropped"
    assert "Co-Founder" in report.dropped_leadership[0]


def test_a_failed_scan_does_not_silently_pass_everyone(conn, config, candidates_dir,
                                                       monkeypatch):
    """If the pages cannot be fetched, rows are unfiltered -- which must be said
    out loud rather than looking like a clean run."""
    from scripts import ingest_candidates
    from scripts.ingest_candidates import ingest

    def boom(domain, names, timeout=15):
        raise ConnectionError("network down")

    monkeypatch.setattr(ingest_candidates.leadership, "scan", boom)
    report = ingest(conn, config, candidates_dir)
    assert any("NOT filtered for seniority" in n for n in report.notes)
