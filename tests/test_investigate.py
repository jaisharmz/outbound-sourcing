"""The investigation loop.

What is being pinned is the behaviour that distinguishes a loop from a channel
sequence: a dead end produces a lead, the frontier is worked best-first, and the
run terminates on both budget and dryness.
"""

from __future__ import annotations

import pytest

from scripts import investigate as I


def test_best_first_not_kind_by_kind():
    inv = I.Investigation("C", "c.com")
    for kind in ("enrichment", "paper", "person", "scholar"):
        inv.push(I.Lead(kind, f"v-{kind}"))
    assert [inv.pop_best().kind for _ in range(4)] == [
        "person", "scholar", "paper", "enrichment"]


def test_a_lead_is_never_followed_twice():
    inv = I.Investigation("C", "c.com")
    assert inv.push(I.Lead("person", "A B", "A B"))
    assert not inv.push(I.Lead("person", "a b", "A B"))


def test_depth_is_bounded():
    """A coauthor graph offers new names indefinitely."""
    inv = I.Investigation("C", "c.com", max_depth=2)
    assert inv.push(I.Lead("person", "X", "X", depth=2))
    assert not inv.push(I.Lead("person", "Y", "Y", depth=3))


def test_dry_streak_stops_the_run(monkeypatch):
    """Dryness alone must terminate, or a graph that keeps yielding runs forever."""
    monkeypatch.setitem(I.STEPS, "person",
                        lambda inv, lead: I.Step(lead, "nothing"))
    inv = I.run("C", "c.com", [I.Lead("person", f"P{i}", f"P{i}") for i in range(20)],
                budget=100, max_dry=3)
    assert len(inv.steps) == 3
    assert "consecutive steps" in inv.stopped_because


def test_budget_stops_a_productive_run(monkeypatch):
    """Budget alone must terminate, or one rich seed spends everything."""
    def always_productive(inv, lead):
        return I.Step(lead, "found more",
                      leads=[I.Lead("person", f"{lead.value}-{i}", "x") for i in range(3)])

    monkeypatch.setitem(I.STEPS, "person", always_productive)
    inv = I.run("C", "c.com", [I.Lead("person", "seed", "seed")],
                budget=7, max_dry=99, max_depth=99)
    assert len(inv.steps) == 7
    assert "budget" in inv.stopped_because


def test_a_step_that_errors_does_not_kill_the_run(monkeypatch):
    def boom(inv, lead):
        raise ValueError("upstream blew up")

    monkeypatch.setitem(I.STEPS, "person", boom)
    inv = I.run("C", "c.com", [I.Lead("person", "A", "A")], budget=3, max_dry=2)
    assert inv.steps and inv.steps[0].outcome.startswith("error:")
    assert "upstream blew up" in inv.steps[0].note


def test_complete_requires_address_and_affiliation():
    inv = I.Investigation("C", "c.com")
    inv.facts.append(I.Fact("email", "A B", "a@c.com", "u", "q"))
    assert not inv.complete("A B")
    inv.facts.append(I.Fact("affiliation", "A B", "C", "u", "q"))
    assert inv.complete("A B")


def test_a_title_is_never_invented():
    """Commit history is evidence about activity, not a claim about a role."""
    inv = I.Investigation("C", "c.com")
    inv.facts.append(I.Fact("email", "A B", "a@c.com", "https://github.com/x",
                            "commit authored by A B <a@c.com>"))
    inv.facts.append(I.Fact("affiliation", "A B", "C", "https://github.com/x", "q"))
    assert inv.complete("A B")
    assert "title" not in inv.person_facts("A B")


def test_the_log_records_what_was_tried_and_what_came_next():
    inv = I.Investigation("C", "c.com")
    inv.steps.append(I.Step(
        I.Lead("homepage", "https://p.test", "A B", "personal page for A B"),
        "page, no address",
        leads=[I.Lead("scholar", "https://scholar.test", "A B", "linked from page")]))
    inv.stopped_because = "budget"
    log = inv.render_log()
    assert "reached from: personal page for A B" in log
    assert "page, no address" in log
    assert "scholar:https://scholar.test" in log


def test_enrichment_is_a_seam_not_an_implementation():
    from scripts import enrichment

    assert enrichment.available() == []
    assert enrichment.resolve("A B", "C") is None
    step = I.step_enrichment(I.Investigation("C", "c.com"),
                             I.Lead("enrichment", "A B", "A B"))
    assert "no paid provider configured" in step.outcome


def test_a_learned_pattern_is_applied_not_just_reported(monkeypatch):
    """infer_pattern measured first.last at 100% across a dozen addresses and
    nothing ever used it. The pattern half of the GitHub channel produced a
    number in a report and no contacts."""
    from scripts import investigate as I

    inv = I.Investigation("Acme", "acme.test")
    monkeypatch.setattr("scripts.hf_org.current_at", lambda c: {
        "ada lovelace": {"name": "Ada Lovelace", "user": "ada"},
        "alan turing": {"name": "Alan Turing", "user": "alan"},
    })

    class Res:
        addresses = {"alan.turing@acme.test": ("Alan Turing", "2026-08-01T00:00:00")}
        status = "ok"

    monkeypatch.setattr("scripts.github_harvest.resolve_org", lambda c, n, d: ("acme", ""))
    monkeypatch.setattr("scripts.github_harvest.harvest_domain",
                        lambda c, n, d, repos=4: Res())
    monkeypatch.setattr("scripts.github_harvest.infer_pattern",
                        lambda a: ("first.last", 1.0, ["x"] * 8))
    monkeypatch.setattr("scripts.config.Config.secrets", lambda self: {})

    step = I.step_domain_pattern(inv, I.Lead("domain_pattern", "acme.test"))
    inferred = [f for f in step.facts if f.kind == "email_inferred"]
    assert [f.value for f in inferred] == ["ada.lovelace@acme.test"]
    # Alan was observed, so he is not re-derived.
    assert all(f.subject != "Alan Turing" for f in inferred)
    assert "measured at 100%" in inferred[0].quote


def test_a_weak_pattern_is_not_applied(monkeypatch):
    """`first` at 55% describes a domain with no convention. Deriving from it
    produces plausible-looking addresses that bounce."""
    from scripts import investigate as I

    inv = I.Investigation("Acme", "acme.test")
    monkeypatch.setattr("scripts.hf_org.current_at", lambda c: {
        "ada lovelace": {"name": "Ada Lovelace", "user": "ada"}})

    class Res:
        addresses = {"alan@acme.test": ("Alan Turing", "2026-08-01T00:00:00")}
        status = "ok"

    monkeypatch.setattr("scripts.github_harvest.resolve_org", lambda c, n, d: ("acme", ""))
    monkeypatch.setattr("scripts.github_harvest.harvest_domain",
                        lambda c, n, d, repos=4: Res())
    monkeypatch.setattr("scripts.github_harvest.infer_pattern",
                        lambda a: ("first", 0.55, ["x"] * 11))
    monkeypatch.setattr("scripts.config.Config.secrets", lambda self: {})

    step = I.step_domain_pattern(inv, I.Lead("domain_pattern", "acme.test"))
    assert not [f for f in step.facts if f.kind == "email_inferred"]


def test_an_inferred_address_is_not_a_complete_contact():
    """'This address exists' and 'an address of this shape would exist if the
    convention holds' are different claims."""
    from scripts import investigate as I

    inv = I.Investigation("Acme", "acme.test")
    inv.facts.append(I.Fact("email_inferred", "Ada", "a.l@acme.test", "u", "q"))
    inv.facts.append(I.Fact("affiliation", "Ada", "Acme", "u", "q"))
    assert not inv.complete("Ada")
    assert inv.inferred_only("Ada")
