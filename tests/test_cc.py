"""CC resolution decides who sees every email. Precedence must be exact."""

from __future__ import annotations

import pytest

from scripts.cc import resolve
from scripts.config import CCConfig
from scripts.suppression import add


def cfg(**over) -> CCConfig:
    base = {
        "default": {"cc": ["group@example.com"], "bcc": ["archive@example.com"]},
        "by_step": {"step3_breakup": {"cc": []}},
        "by_campaign": {"frontier-labs": {"cc": ["group@example.com", "peer@example.com"]}},
        "by_domain": {"target.test": {"cc": ["special@example.com"]}},
        "merge": False,
    }
    base.update(over)
    return CCConfig.model_validate(base)


def test_default_applies_when_nothing_else_matches():
    r = resolve(cfg(), domain="other.test", campaign="default", step="step1_initial")
    assert r.cc == ["group@example.com"]
    assert r.cc_source == "default"


def test_step_overrides_default():
    r = resolve(cfg(), domain="other.test", campaign="default", step="step3_breakup")
    assert r.cc == []
    assert r.cc_source == "step:step3_breakup"


def test_campaign_overrides_step():
    r = resolve(cfg(), domain="other.test", campaign="frontier-labs", step="step3_breakup")
    assert r.cc == ["group@example.com", "peer@example.com"]
    assert r.cc_source == "campaign:frontier-labs"


def test_domain_overrides_campaign():
    r = resolve(cfg(), domain="target.test", campaign="frontier-labs", step="step3_breakup")
    assert r.cc == ["special@example.com"]
    assert r.cc_source == "domain:target.test"


def test_all_four_levels_in_one_ordering():
    """domain > campaign > step > default, checked as a single chain."""
    c = cfg()
    assert resolve(c, domain="target.test", campaign="frontier-labs",
                   step="step3_breakup").cc_source.startswith("domain")
    assert resolve(c, domain="none.test", campaign="frontier-labs",
                   step="step3_breakup").cc_source.startswith("campaign")
    assert resolve(c, domain="none.test", campaign="none",
                   step="step3_breakup").cc_source.startswith("step")
    assert resolve(c, domain="none.test", campaign="none",
                   step="step1_initial").cc_source == "default"


def test_empty_list_is_a_decision_not_an_absence():
    """`cc: []` on a step drops the CC; it must not fall through to default."""
    r = resolve(cfg(), step="step3_breakup", campaign="default")
    assert r.cc == []
    assert r.cc_source != "default"


def test_bcc_resolves_independently_of_cc():
    r = resolve(cfg(), domain="target.test", campaign="default", step="step1_initial")
    assert r.cc == ["special@example.com"]     # from the domain rule
    assert r.bcc == ["archive@example.com"]    # domain rule says nothing about bcc
    assert r.bcc_source == "default"


def test_merge_mode_unions_every_matching_level():
    r = resolve(cfg(merge=True), domain="target.test", campaign="frontier-labs",
                step="step1_initial")
    assert r.cc == ["special@example.com", "group@example.com", "peer@example.com"]
    assert "merge(" in r.cc_source


def test_subdomain_falls_back_to_the_registrable_domain():
    r = resolve(cfg(), domain="mail.target.test", campaign="default", step="step1_initial")
    assert r.cc == ["special@example.com"]


def test_recipient_count_covers_cc_and_bcc():
    r = resolve(cfg(), domain="other.test", campaign="default", step="step1_initial")
    assert r.recipient_count == 2  # one cc + one bcc, excluding the To: address


def test_suppressed_cc_never_appears_on_a_send(conn):
    """A CC address goes through the same suppression check as a recipient."""
    add(conn, "email", "group@example.com", "opted out")
    r = resolve(cfg(), domain="other.test", campaign="default", step="step1_initial", conn=conn)
    assert r.cc == []
    assert any("group@example.com" in s for s in r.suppressed)


def test_suppressed_domain_removes_matching_cc(conn):
    add(conn, "domain", "example.com", "domain-wide opt out")
    r = resolve(cfg(), domain="other.test", campaign="frontier-labs", step="step1_initial",
                conn=conn)
    # Both CCs and the BCC are at the suppressed domain, so all three go.
    assert r.cc == []
    assert r.bcc == []
    assert set(r.suppressed) == {
        "group@example.com", "peer@example.com", "archive@example.com"
    }
