"""Harvester. Fixtures only -- nothing here touches GitHub."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.github_harvest import (
    DomainResult, RateLimiter, apply_pattern, infer_pattern, is_person_address,
)


def iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------- filtering


@pytest.mark.parametrize("email,name", [
    ("12345+user@users.noreply.github.com", "user"),
    ("dependabot[bot]@users.noreply.github.com", "dependabot[bot]"),
    ("svc-template-updater@anyscale.com", "svc-template-updater"),
    ("lattice-sdk-admin@anduril.com", "anduril-gh-bot"),
    ("no-reply@example.com", ""),
    ("ci@example.com", ""),
])
def test_bots_and_noreply_are_not_people(email, name):
    assert not is_person_address(email, name)


@pytest.mark.parametrize("email,name", [
    ("aydin@anyscale.com", "Aydin Abiar"),
    ("max.deliso@abridge.com", "Max DeLiso"),
    ("chris@bayesianhealth.com", "Chris Hayen"),
])
def test_real_addresses_survive(email, name):
    assert is_person_address(email, name)


# ---------------------------------------------------------------- staleness


def test_a_recent_commit_is_a_sendable_contact():
    r = DomainResult("X", "x.test", addresses={"a@x.test": ("A Person", iso(30))})
    assert "a@x.test" in r.fresh
    assert r.stale == {}


def test_an_old_commit_is_pattern_evidence_only():
    """A commit proves an address existed when it landed, not that the person is
    still there. Anyscale: three findable founders, company acquired."""
    r = DomainResult("X", "x.test", addresses={"a@x.test": ("A Person", iso(1200))})
    assert r.fresh == {}
    assert "a@x.test" in r.stale


def test_fresh_and_stale_partition_cleanly():
    r = DomainResult("X", "x.test", addresses={
        "new@x.test": ("New Person", iso(10)),
        "old@x.test": ("Old Person", iso(900)),
    })
    assert set(r.fresh) == {"new@x.test"}
    assert set(r.stale) == {"old@x.test"}


# ---------------------------------------------------------------- rate limit


def test_limiter_pauses_only_near_the_floor():
    lim = RateLimiter(floor=25)
    lim.remaining, lim.reset_at = 500, __import__("time").time() + 60
    assert lim.should_pause() == 0
    lim.remaining = 3
    assert lim.should_pause() > 0


def test_limiter_reads_the_budget_off_the_response():
    lim = RateLimiter()
    lim.note({"X-RateLimit-Remaining": "17", "X-RateLimit-Reset": "1900000000"})
    assert lim.remaining == 17


def test_throttled_is_not_reported_as_absence():
    """An unauthenticated probe once reported `no repos` for 78 of 88 companies,
    which read as a finding and was a rate limit."""
    assert DomainResult("X", "x.test", status="throttled").status != "no_public_repos"


# ---------------------------------------------------------------- patterns


def test_infers_first_dot_last():
    obs = {"max.deliso@abridge.com": ("Max DeLiso", iso(1)),
           "blake.tsuhako@abridge.com": ("Blake Tsuhako", iso(1))}
    pattern, confidence, used = infer_pattern(obs)
    assert pattern == "first.last"
    assert confidence == 1.0
    assert len(used) == 2


def test_infers_first_only():
    obs = {"chris@bayesianhealth.com": ("Chris Hayen", iso(1)),
           "aydin@anyscale.com": ("Aydin Abiar", iso(1))}
    assert infer_pattern(obs)[0] == "first"


def test_infers_flast():
    obs = {"oagrawal@anduril.com": ("Om Agrawal", iso(1)),
           "vpuri@anduril.com": ("Vik Puri", iso(1))}
    assert infer_pattern(obs)[0] == "flast"


def test_no_pattern_when_names_are_unusable():
    assert infer_pattern({"x@y.test": ("gh-user", iso(1))})[0] is None


def test_a_learned_pattern_unlocks_a_new_name():
    """This is most of the value: one confirmed convention turns every name found
    elsewhere into a candidate address."""
    assert apply_pattern("first.last", "Jane Quinn Roberts", "abridge.com") == \
        "jane.roberts@abridge.com"
    assert apply_pattern("flast", "Om Agrawal", "anduril.com") == "oagrawal@anduril.com"


def test_apply_pattern_refuses_a_single_token_name():
    assert apply_pattern("first.last", "Cher", "x.test") is None


# ------------------------------------------- handles are not names

@pytest.mark.parametrize("name,domain", [
    ("oagrawal-anduril", "anduril.com"),      # org-suffixed login
    ("piotr-reducto", "reducto.ai"),
    ("rasa-aadlv", "rasa.com"),               # org-prefixed login
    ("rasabot", "rasa.com"),                  # bot without a marker
    ("flock-auditor", "flocksafety.com"),
    ("jymmi", "foursquare.com"),              # single lowercase token
    ("danc", "rasa.com"),
])
def test_logins_are_not_usable_names(name, domain):
    from scripts.github_harvest import name_is_usable
    assert not name_is_usable(name, domain)


@pytest.mark.parametrize("name,domain", [
    ("Tom Bocklisch", "rasa.com"),
    ("Nicholas Pena", "foursquare.com"),
    ("Siddhant Nandkishor Pagari", "reducto.ai"),
    ("Matias Varnum", "skydio.com"),
])
def test_real_names_are_usable(name, domain):
    from scripts.github_harvest import name_is_usable
    assert name_is_usable(name, domain)


def test_a_partial_name_is_not_usable():
    """'Adam S' and 'A. Rager' cannot be matched to a title with confidence."""
    from scripts.github_harvest import name_is_usable
    assert not name_is_usable("Adam S", "foursquare.com")
    assert not name_is_usable("A. Rager", "skydio.com")


def test_bot_named_accounts_are_rejected_as_addresses():
    assert not is_person_address("auditor@flocksafety.com", "flock-auditor")
    assert not is_person_address("ci@rasa.com", "rasabot")
