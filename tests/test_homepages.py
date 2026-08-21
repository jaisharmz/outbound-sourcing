"""Homepage fetching. The point is that a failed fetch is a fact about the fetch,
never a fact about the company."""

from __future__ import annotations

import pytest

from scripts.homepages import classify, embedded_text, visible_text


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_extracts_visible_text_and_skips_script():
    p = page("<p>We train models.</p><script>var x = 'not copy';</script>")
    text = visible_text(p)
    assert "We train models." in text
    assert "not copy" not in text


def test_meta_description_is_kept():
    p = page("<p>hi</p>", head='<meta name="description" content="AI for radiology">')
    assert "AI for radiology" in visible_text(p)


def test_meaningful_text_is_ok():
    body = "<p>" + ("We build models for protein design. " * 12) + "</p>"
    status, _ = classify(page(body), visible_text(page(body)), "")
    assert status == "ok"


def test_holding_page_is_not_a_fail():
    p = page("<h1>Coming soon</h1>")
    status, _ = classify(p, visible_text(p), "")
    assert status == "holding"


def test_bot_challenge_is_blocked():
    p = page("<h1>Just a moment...</h1><p>Checking your browser</p>")
    status, _ = classify(p, visible_text(p), "")
    assert status == "blocked"


def test_client_rendered_shell_is_js_shell_not_fail():
    p = page('<div id="root"></div>' + "<script></script>" * 12)
    status, detail = classify(p, visible_text(p), "")
    assert status == "js_shell"
    assert "client-rendered" in detail or "chars of text" in detail


def test_embedded_payload_is_recovered_before_calling_a_page_empty():
    """The standing rule: content may be in the source even when the page renders
    thin. Check before concluding anything."""
    blob = ('{"hero":"We build frontier models for the visual world and ship them to '
            'developers. We train them ourselves on our own infrastructure rather than '
            'fine-tuning somebody else\'s checkpoints, and we publish the research that '
            'comes out of it because the field moves faster when people can read it."}')
    p = page(f'<div id="root"></div><script id="__NEXT_DATA__">{blob}</script>')
    embedded = embedded_text(p)
    assert "frontier models" in embedded
    status, detail = classify(p, visible_text(p), embedded)
    assert status == "ok"
    assert "embedded" in detail


def test_data_attribute_payload_is_recovered():
    """Real pages escape the quotes, which is how a16z ships 855 companies."""
    long_json = "[" + ",".join(
        "{&quot;name&quot;:&quot;Company %d&quot;}" % i for i in range(30)
    ) + "]"
    p = page(f'<div data-companies="{long_json}"></div>')
    assert "Company 1" in embedded_text(p)
