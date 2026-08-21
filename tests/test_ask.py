"""When the loop stops to ask, and what the question looks like.

The cost being managed is the operator's attention. A question that takes a
paragraph to read costs more than the decision is worth; one asked twelve times
teaches them to stop reading; one whose answer is already implied by what they
asked for should never have been asked.
"""

from __future__ import annotations

import pytest

from scripts import ask as A


def test_a_question_is_short_enough_to_answer_at_a_glance():
    """One line of context, one question, two or three one-word answers."""
    questions = [
        A.company_found(4, "Modal Labs", "Baseten"),
        A.industry_adjacent("speculative decoding", "Fireworks AI"),
        A.senior_person("Priya Raghavan", "VP Engineering", "Fireworks"),
        A.ambiguous_claim("Kaitlyn Zhou", "page shows a talk, not employment"),
    ]
    for q in questions:
        assert "\n" not in q.context, "context must be one line"
        assert len(q.context) <= 110, f"context too long: {q.context}"
        assert q.ask.endswith("?")
        assert len(q.ask) <= 60, f"question too long: {q.ask}"
        assert 2 <= len(q.options) <= 3
        for opt in q.options:
            assert " " not in opt, f"answers must be one word: {opt!r}"


def test_always_stops_the_same_question_recurring():
    """Asked twelve times, the twelfth answer is not a considered one."""
    asked = []

    def answer(q):
        asked.append(q.context)
        return "always"

    s = A.Session()
    d1 = s.resolve(A.company_found(4, "Modal Labs", "Baseten"), answer)
    d2 = s.resolve(A.company_found(2, "Replicate", "Modal"), answer)
    assert d1.asked and d1.yes
    assert not d2.asked and d2.yes
    assert len(asked) == 1, "the second question should not have been asked"
    assert "earlier in the run" in d2.why


def test_never_settles_the_class_the_other_way():
    s = A.Session()
    s.resolve(A.senior_person("A", "VP", "X"), lambda q: "never")
    d = s.resolve(A.senior_person("B", "Director", "Y"), lambda q: pytest.fail("asked"))
    assert not d.yes and not d.asked


def test_auto_expands_without_asking_but_not_silently():
    """`auto` is unattended, not invisible: every decision still appears."""
    s = A.Session(autonomy=A.AUTO)
    d = s.resolve(A.company_found(4, "Modal Labs", "Baseten"),
                  lambda q: pytest.fail("auto must not ask"))
    assert d.yes and not d.asked and d.why == "autonomy: auto"
    assert any("Modal Labs" in line for line in s.summary())


def test_strict_refuses_to_wander_and_says_so():
    s = A.Session(autonomy=A.STRICT)
    d = s.resolve(A.company_found(4, "Modal Labs", "Baseten"),
                  lambda q: pytest.fail("strict must not ask"))
    assert not d.yes and d.why == "autonomy: strict"
    assert any("Modal Labs" in line for line in s.summary())


def test_autonomy_never_silences_a_judgment_call():
    """auto and strict govern expansion. Seniority and ambiguity are judgments
    about a specific person and a specific claim, and are always asked."""
    for mode in (A.AUTO, A.STRICT):
        s = A.Session(autonomy=mode)
        asked = []
        s.resolve(A.senior_person("P", "VP Engineering", "X"),
                  lambda q: asked.append(q) or "n")
        s.resolve(A.ambiguous_claim("Q", "a talk, not employment"),
                  lambda q: asked.append(q) or "n")
        assert len(asked) == 2, f"{mode} skipped a judgment call"


def test_the_summary_states_the_outcome_not_the_mechanics():
    out = A.run_summary(
        drafted=[("A", "Baseten"), ("B", "Baseten"), ("C", "Modal Labs")],
        skipped=[("D", "on the founders page")],
        read_first=["2 contacts have title_status: unknown"])
    assert out.startswith("3 drafts in your Gmail")
    assert "  2  Baseten" in out
    assert "1 skipped:" in out
    assert "on the founders page" in out
    assert "outbound mark-sent --all" in out
    # No step-by-step mechanics.
    for noise in ("fetching", "step ", "http", "traversal"):
        assert noise not in out.lower()


def test_the_summary_says_something_when_nothing_is_flagged():
    out = A.run_summary(drafted=[("A", "X")], skipped=[], read_first=[])
    assert "Read a couple anyway" in out
