"""Industry suggestions. Judgment about what belongs to a field stays with the
model; this is the display and the selection parsing."""

from __future__ import annotations

import pytest

from scripts.suggest import Suggestion, SelectionError, parse_selection, render


def test_funding_is_unknown_rather_than_guessed():
    """Several fund sources give a name and nothing else."""
    assert Suggestion(name="X").funding_line == "Funding unknown"
    assert "Series B" in Suggestion(name="X", stage="Series B", raised="$52M").funding_line


def test_an_investor_description_is_labelled_as_the_investor_s():
    """Kaedim's blurb read 'game-ready on-demand 3D assets'; its own homepage
    opens 'AI-powered 3D asset creation'."""
    out = render([Suggestion(name="X", description="blurb", description_source="investor")], "Topic")
    assert "description is the investor's" in out


def test_a_homepage_description_carries_no_caveat():
    out = render([Suggestion(name="X", description="own words", description_source="homepage")], "Topic")
    assert "investor's" not in out


def test_missing_description_says_so():
    assert "no description available" in render([Suggestion(name="X")], "Topic")


def test_a_warm_route_is_flagged_in_the_list():
    out = render([Suggestion(name="X", fund="kleiner-perkins", relationship="fellowship")], "Topic")
    assert "warm route available" in out and "intro beats a cold email" in out


def test_the_source_fund_is_shown():
    assert "via greylock" in render([Suggestion(name="X", fund="greylock")], "Topic")


def test_list_caps_and_offers_more():
    items = [Suggestion(name=f"C{i}") for i in range(40)]
    out = render(items, "Topic", limit=15)
    assert "C14" in out and "C15" not in out
    assert "15 of 40 shown" in out and "say 'more'" in out


def test_no_more_prompt_when_everything_fits():
    assert "say 'more'" not in render([Suggestion(name="C1")], "Topic", limit=15)


@pytest.mark.parametrize("text,expected", [
    ("3", [2]), ("1,4,7", [0, 3, 6]), ("1-5", [0, 1, 2, 3, 4]),
    ("all", list(range(8))), ("2 5", [1, 4]), ("1-3,6", [0, 1, 2, 5]),
])
def test_selection_forms(text, expected):
    assert parse_selection(text, 8) == expected


@pytest.mark.parametrize("bad", ["", "0", "9", "5-1", "banana", "1-99"])
def test_bad_selections_are_refused(bad):
    with pytest.raises(SelectionError):
        parse_selection(bad, 8)


def test_selection_deduplicates():
    assert parse_selection("1,1,2-3,3", 5) == [0, 1, 2]
