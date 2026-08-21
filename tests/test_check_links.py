"""A linked document that asks the recipient to request access is worse than no
link: they click, are told to ask permission, and the email reads as careless.
So reachability is a send gate, and these tests run it without a network."""

from __future__ import annotations

import pytest

from scripts import check_links as CL
from scripts.config import Config


def _stub(monkeypatch, status: str = "ok", detail: str = "serves a file directly"):
    monkeypatch.setattr(CL, "check_url", lambda url, timeout=30: (status, detail))


def test_unchecked_links_block_a_campaign(conn, config: Config):
    blockers = CL.gate(conn, config)
    assert blockers and "never been checked" in blockers[0]
    assert "outbound check-links" in blockers[0]


def test_passing_check_clears_the_gate(conn, config: Config, monkeypatch):
    _stub(monkeypatch)
    CL.check_all(conn, config)
    assert CL.gate(conn, config) == []


def test_a_permission_wall_blocks(conn, config: Config, monkeypatch):
    _stub(monkeypatch, "permission_wall", "the page asks the visitor to request access")
    CL.check_all(conn, config)
    blockers = CL.gate(conn, config)
    assert blockers
    assert any("permission_wall" in b and "request access" in b for b in blockers)


def test_changing_a_url_invalidates_an_earlier_pass(conn, config_root, monkeypatch):
    """Otherwise a link swapped in after the check ships unverified -- the exact
    case the gate exists for."""
    _stub(monkeypatch)
    config = Config(config_root)
    CL.check_all(conn, config)
    assert CL.gate(conn, config) == []

    path = config_root / "sequence.yaml"
    path.write_text(path.read_text().replace("https://example.com/a.pdf",
                                             "https://example.com/replaced.pdf"))
    changed = Config(config_root)
    blockers = CL.gate(conn, changed)
    assert blockers and "changed since" in blockers[0]


def test_send_gate_refuses_while_a_link_is_unreachable(conn, config: Config, monkeypatch):
    """The gate is only worth having if the send path actually consults it."""
    from scripts import send_queue

    _stub(monkeypatch, "login_wall", "redirected to a sign-in page")
    CL.check_all(conn, config)
    campaign = next(iter(config.campaigns.campaigns))
    problems = send_queue.gate(conn, config, campaign, "primary")
    assert any("not publicly reachable" in p for p in problems)


@pytest.mark.parametrize("body,disposition,expect", [
    (b"%PDF-1.7 ...", "", "ok"),
    (b"<html>You need access to view this file. Request access</html>", "", "permission_wall"),
    (b"<html>nothing here</html>", "", "dead"),
    (b"<html>whatever</html>", 'attachment; filename="x.pdf"', "ok"),
])
def test_classification(monkeypatch, body, disposition, expect):
    class R:
        headers = {"Content-Disposition": disposition}
        def read(self, n): return body
        def geturl(self): return "https://real-host.test/a.pdf"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(CL.urllib.request, "urlopen", lambda *a, **k: R())
    # Not an example.com URL: those short-circuit as placeholders before any
    # request is made, which is a different test.
    assert CL.check_url("https://real-host.test/a.pdf")[0] == expect


def test_a_removed_link_does_not_block_forever(conn, config_root, monkeypatch):
    """A URL dropped from the config leaves its last check behind, carrying the
    fingerprint of the set it belonged to. Reading those rows made removing a
    link block the gate permanently -- no later check updates a row for a URL
    that is no longer looked up."""
    _stub(monkeypatch)
    config = Config(config_root)
    CL.check_all(conn, config)
    assert CL.gate(conn, config) == []

    path = config_root / "sequence.yaml"
    text = path.read_text()
    start = text.index("      - name: Example large document")
    end = text.index("reply_templates:")
    path.write_text(text[:start] + "\n" + text[end:])

    trimmed = Config(config_root)
    blockers = CL.gate(conn, trimmed)
    assert blockers, "removing a link should require a re-check"
    CL.check_all(conn, trimmed)
    assert CL.gate(conn, trimmed) == [], "the tombstone row must not block after re-check"



def test_an_example_url_is_unfinished_setup_not_a_dead_link():
    """A fresh install ships example.com URLs. Reporting those as dead shows a
    new user a FAIL they did not cause on their first run, which is how people
    learn to ignore FAILs."""
    status, detail = CL.check_url("https://example.com/a.pdf")
    assert status == "placeholder"
    assert "replace it with your own" in detail

    for url in ("https://example.org/x", "https://sub.example.test/y",
                "http://localhost:8000/z"):
        assert CL.is_placeholder(url), url
    assert not CL.is_placeholder("https://drive.google.com/uc?id=abc")


def test_a_placeholder_still_blocks_a_campaign(conn, config, monkeypatch):
    """It must not send -- you cannot email a stranger a link to example.com --
    but the message says unfinished setup rather than broken link."""
    _stub(monkeypatch, "placeholder", "an example URL from config.example")
    CL.check_all(conn, config)
    blockers = CL.gate(conn, config)
    assert blockers
    assert any("still the example URL" in b for b in blockers)
    assert not any("not publicly reachable" in b for b in blockers)
