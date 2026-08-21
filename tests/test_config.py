"""Config must fail fast and say what to fix. A typo that sends 500 emails is
the failure mode this file exists to prevent."""

from __future__ import annotations

import pytest

from scripts.config import Config
from scripts.errors import ConfigError


def test_example_config_loads(config: Config):
    assert config.persona.name
    assert config.sequence.steps[0].id == "step1_initial"
    assert len(config.dorks) == 5


def test_unknown_key_suggests_the_right_one(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text().replace("daily_cap:", "dailycap:"))
    with pytest.raises(ConfigError) as exc:
        Config(config_root)
    msg = str(exc.value)
    assert "unknown key `dailycap`" in msg
    assert "did you mean `daily_cap`?" in msg


def test_unknown_nested_key_names_its_own_level(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text().replace("  min_seconds: 90", "  min_second: 90"))
    with pytest.raises(ConfigError) as exc:
        Config(config_root)
    msg = str(exc.value)
    assert "unknown key `min_second`" in msg
    assert "inter_send_delay" in msg
    assert "did you mean `min_seconds`?" in msg


def test_missing_template_is_caught_before_any_send(config_root):
    (config_root / "templates" / "step2_bump.md").unlink()
    with pytest.raises(ConfigError, match="missing template"):
        Config(config_root)


def test_missing_attachment_with_no_url_is_caught_before_any_send(config_root):
    """A vanished file is recoverable if the document also carries a url; if it
    does not, the send has nothing to carry and must fail at load."""
    from pathlib import Path

    path = config_root / "sequence.yaml"
    path.write_text(path.read_text().replace("        url: https://example.com/a.pdf\n", ""))
    cfg = Config(config_root)
    target = next(iter(cfg.sequence.attachment_sets.values()))
    (Path(cfg.campaign.attachments_root) / target.dir / target.documents[0].file).unlink()
    with pytest.raises(ConfigError, match="has no url to fall back to"):
        Config(config_root)


def test_missing_attachment_falls_back_to_its_url(config_root):
    from pathlib import Path
    from scripts.templates import resolve_documents

    cfg = Config(config_root)
    target = next(iter(cfg.sequence.attachment_sets.values()))
    (Path(cfg.campaign.attachments_root) / target.dir / target.documents[0].file).unlink()
    cfg = Config(config_root)                      # must not raise
    step = next(s for s in cfg.sequence.steps if s.attachment_set)
    _attachments, links = resolve_documents(cfg, step)
    assert any(name == target.documents[0].name for name, _ in links)


def test_undefined_attachment_set_is_rejected(config_root):
    path = config_root / "sequence.yaml"
    path.write_text(path.read_text().replace("attachment_set: first_touch",
                                             "attachment_set: does_not_exist"))
    with pytest.raises(ConfigError, match="does_not_exist"):
        Config(config_root)


def test_sending_window_must_be_ordered(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text().replace('start: "08:00"', 'start: "17:00"'))
    with pytest.raises(ConfigError, match="must be before"):
        Config(config_root)


def test_unknown_day_is_rejected(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text().replace("days: [tue, wed, thu]", "days: [tues, wed]"))
    with pytest.raises(ConfigError, match="unknown day"):
        Config(config_root)


def test_duplicate_mailbox_id_is_rejected(config_root):
    path = config_root / "mailboxes.yaml"
    path.write_text(path.read_text().replace("id: primary", "id: console"))
    with pytest.raises(ConfigError, match="duplicate mailbox id"):
        Config(config_root)


def test_mailbox_from_and_reply_to_are_separate(config: Config):
    mb = config.mailboxes.get("primary")
    assert mb.from_.address == "you@example.com"
    assert mb.reply_to == "you@your-institution.edu"
    assert mb.from_.header() == "Your Name <you@example.com>"


def test_oversized_document_becomes_a_link_not_an_error(config_root):
    """Base64 inflates by 4/3 and many gateways reject above 5 MB, so a heavy
    document must not ride along -- but it must not stop the send either. It
    moves to the link list, which is the whole point of carrying both."""
    from pathlib import Path
    from scripts.templates import resolve_documents

    cfg = Config(config_root)
    root = Path(cfg.campaign.attachments_root)
    (root / "_first_email" / "example_document_a.pdf").write_bytes(b"x" * 8_000_000)

    cfg = Config(config_root)                      # must not raise
    step = next(s for s in cfg.sequence.steps if s.attachment_set)
    attachments, links = resolve_documents(cfg, step)
    assert "example_document_a.pdf" not in [a.name for a in attachments]
    assert any("Example small document" == name for name, _ in links)
    assert cfg.preflight("campaign") == []


def test_oversized_document_with_no_url_is_a_hard_failure(config_root):
    """Linking is the escape hatch. Without one there is nothing to fall back
    to, and silently dropping the document would send a half-made pitch."""
    from pathlib import Path
    from scripts.templates import resolve_documents

    path = config_root / "sequence.yaml"
    path.write_text(path.read_text().replace("        url: https://example.com/a.pdf\n", ""))
    cfg = Config(config_root)
    (Path(cfg.campaign.attachments_root) / "_first_email" / "example_document_a.pdf"
     ).write_bytes(b"x" * 8_000_000)

    cfg = Config(config_root)
    step = next(s for s in cfg.sequence.steps if s.attachment_set)
    with pytest.raises(ConfigError) as exc:
        resolve_documents(cfg, step)
    msg = str(exc.value)
    assert "no url to fall back to" in msg
    assert "on the wire" in msg
    assert "max_attachment_bytes" in msg           # names the binding ceiling
    assert any("no url to fall back to" in b for b in cfg.preflight("campaign"))


def test_attachment_limit_is_configurable(config_root):
    from pathlib import Path

    cfg = Config(config_root)
    (Path(cfg.campaign.attachments_root) / "_first_email" / "example_document_a.pdf"
     ).write_bytes(b"x" * 8_000_000)
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + "\nmax_attachment_bytes: 20000000\n")
    assert Config(config_root).campaign.max_attachment_bytes == 20_000_000


def test_wire_size_accounts_for_base64():
    from scripts.config import wire_size
    assert wire_size(3) == 4
    assert wire_size(14_720_000) > 19_000_000


def test_clean_config_has_no_blockers(config: Config):
    assert config.preflight("campaign") == []


def test_enabled_mailbox_with_a_placeholder_from_blocks(config_root):
    path = config_root / "mailboxes.yaml"
    text = path.read_text().replace(
        "      address: you@example.com", "      address: you@SENDING-DOMAIN-TBD.com"
    ).replace("    enabled: false", "    enabled: true")
    path.write_text(text)
    blockers = Config(config_root).preflight("campaign")
    assert any("placeholder" in b for b in blockers)


def test_disabled_mailbox_placeholder_does_not_block(config_root):
    """The console mailbox is disabled, so a placeholder in it is inert."""
    path = config_root / "mailboxes.yaml"
    text = path.read_text().replace("""  - id: console
    provider: console
    from:
      name: Your Name
      address: you@example.com""", """  - id: console
    provider: console
    from:
      name: Your Name
      address: you@SENDING-DOMAIN-TBD.com""")
    path.write_text(text)
    assert not any("placeholder" in b for b in Config(config_root).preflight("campaign"))


def _oversize(config_root, mb: int = 8) -> None:
    from pathlib import Path
    cfg = Config(config_root)
    (Path(cfg.campaign.attachments_root) / "_first_email" / "example_document_a.pdf"
     ).write_bytes(b"x" * (mb * 1_000_000))


def test_raised_load_cap_lets_a_heavy_set_load(config_root):
    """A loosened ceiling exists so a heavy set can go out on test sends, which
    only ever reach an address the operator controls."""
    _oversize(config_root)
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + "\nmax_attachment_bytes: 20000000\n")
    Config(config_root)   # must not raise


def test_raised_load_cap_does_not_leak_into_campaigns(config_root):
    """The whole point of the split: raising the test-send ceiling must not
    raise what strangers receive. The split is computed against the stricter of
    the two, so the leak is impossible rather than merely detected."""
    from scripts.templates import resolve_documents
    from scripts.config import wire_size

    _oversize(config_root)
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + "\nmax_attachment_bytes: 20000000\n")

    cfg = Config(config_root)
    assert cfg.campaign.max_attachment_bytes == 20_000_000
    step = next(s for s in cfg.sequence.steps if s.attachment_set)
    attachments, links = resolve_documents(cfg, step)
    total = wire_size(sum(a.size for a in attachments))
    assert total <= cfg.campaign.campaign_max_attachment_bytes
    assert "example_document_a.pdf" not in [a.name for a in attachments]
    assert cfg.preflight("campaign") == []

def test_campaign_gate_is_independently_configurable(config_root):
    _oversize(config_root)
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text()
                    + "\nmax_attachment_bytes: 20000000\n"
                    + "campaign_max_attachment_bytes: 20000000\n")
    assert Config(config_root).preflight("campaign") == []


def test_test_mode_preflight_ignores_the_campaign_attachment_gate(config_root):
    _oversize(config_root)
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + "\nmax_attachment_bytes: 20000000\n")
    cfg = Config(config_root)
    assert not any("campaign_max_attachment_bytes" in b for b in cfg.preflight("test"))


def test_within_limits_config_has_no_attachment_blocker(config: Config):
    assert not any("attachment" in b for b in config.preflight("campaign"))


# ---------------------------------------------------------------- campaigns


@pytest.fixture
def two_campaigns(config_root):
    (config_root / "templates" / "startup").mkdir(exist_ok=True)
    (config_root / "templates" / "frontier-lab").mkdir(exist_ok=True)
    src = (config_root / "templates" / "step1_initial.md").read_text()
    (config_root / "templates" / "startup" / "step1_initial.md").write_text(src)
    (config_root / "templates" / "frontier-lab" / "step1_initial.md").write_text(
        "---\nsubject: \"[WRITE THIS SUBJECT]\"\n---\n[WRITE THIS COPY]\n"
    )
    (config_root / "campaigns.yaml").write_text(
        "campaigns:\n"
        "  startup:\n    tiers: [startup]\n    templates_dir: templates/startup\n"
        "  frontier-lab:\n    tiers: [frontier-lab]\n    templates_dir: templates/frontier-lab\n"
    )
    return Config(config_root)


def test_tier_maps_to_a_campaign(two_campaigns):
    assert two_campaigns.campaigns.for_tier("startup") == "startup"
    assert two_campaigns.campaigns.for_tier("frontier-lab") == "frontier-lab"
    assert two_campaigns.campaigns.for_tier("academic") is None


def test_campaign_templates_override_the_shared_one(two_campaigns):
    step = two_campaigns.sequence.steps[0]
    startup = two_campaigns.template_path(step, "startup")
    frontier = two_campaigns.template_path(step, "frontier-lab")
    assert startup.parent.name == "startup"
    assert frontier.parent.name == "frontier-lab"
    assert startup.read_text() != frontier.read_text()


def test_campaign_falls_back_to_the_shared_template_per_file(two_campaigns):
    """A campaign only overrides the templates it actually changes."""
    step2 = two_campaigns.sequence.steps[1]
    assert two_campaigns.template_path(step2, "startup").parent.name == "templates"


def test_unknown_campaign_is_a_clear_error(two_campaigns):
    with pytest.raises(ConfigError, match="no campaign named"):
        two_campaigns.campaigns.get("nope")


def test_template_hash_differs_per_campaign(two_campaigns):
    from scripts.templates import template_hash
    assert template_hash(two_campaigns, "startup") != template_hash(two_campaigns, "frontier-lab")


def test_a_stub_template_blocks_its_campaign(two_campaigns):
    """An unwritten email must not be sendable."""
    blockers = two_campaigns.preflight("campaign")
    assert any("frontier-lab" in b and "stub" in b for b in blockers)
    assert not any("'startup'" in b and "stub" in b for b in blockers)


def test_a_stub_does_not_block_a_test_send(two_campaigns):
    assert not any("stub" in b for b in two_campaigns.preflight("test"))


def test_missing_campaign_template_is_caught_at_load(config_root):
    (config_root / "campaigns.yaml").write_text(
        "campaigns:\n  ghost:\n    tiers: [x]\n    templates_dir: templates/ghost\n"
    )
    (config_root / "templates" / "step1_initial.md").unlink()
    with pytest.raises(ConfigError, match="missing template"):
        Config(config_root)


def test_campaigns_yaml_is_optional(config_root):
    (config_root / "campaigns.yaml").unlink()
    cfg = Config(config_root)
    assert cfg.campaigns.campaigns == {}
    assert cfg.steps_for("anything") == cfg.sequence.steps


def test_rendering_uses_the_campaign_template(two_campaigns):
    from scripts.templates import render
    contact = {"first_name": "Ada", "last_name": "L", "name": "Ada L",
               "title": "RS", "email": "a@b.test", "personalization": None}
    account = {"name": "Target", "domain": "b.test"}
    email = render(two_campaigns, two_campaigns.sequence.steps[0], contact=contact,
                   account=account, to="a@b.test", campaign="frontier-lab")
    assert "[WRITE THIS COPY]" in email.body
    assert email.campaign == "frontier-lab"


def test_campaigns_route_on_depth_as_well_as_tier(config_root):
    (config_root / "templates" / "applied").mkdir(exist_ok=True)
    src = (config_root / "templates" / "step1_initial.md").read_text()
    (config_root / "templates" / "applied" / "step1_initial.md").write_text(src)
    (config_root / "campaigns.yaml").write_text(
        "campaigns:\n"
        "  startup:\n    tiers: [startup]\n    ai_depth: [builds]\n"
        "  applied-ai:\n    ai_depth: [applies]\n    templates_dir: templates/applied\n"
    )
    cfg = Config(config_root)
    assert cfg.campaigns.for_depth("builds") == "startup"
    assert cfg.campaigns.for_depth("applies") == "applied-ai"
    assert cfg.campaigns.for_depth(None) is None
    assert cfg.campaigns.depth_routes() == {"builds": "startup", "applies": "applied-ai"}


def test_a_placeholder_link_blocks_its_campaign(two_campaigns):
    """A linked document with no URL is an unwritten email by another route."""
    two_campaigns.sequence.steps[0].links = {"Portfolio": "[DRIVE URL NEEDED]"}
    blockers = two_campaigns.preflight("campaign")
    assert any("DRIVE URL NEEDED" in b and "Portfolio" in b for b in blockers)


def test_a_real_link_does_not_block(two_campaigns):
    two_campaigns.sequence.steps[0].links = {"Portfolio": "https://drive.example.test/abc"}
    assert not any("Portfolio" in b for b in two_campaigns.preflight("campaign"))


def test_verification_chain_has_no_paid_tier(config: Config):
    assert config.campaign.verification.chain == ["mx", "smtp"]
    assert not hasattr(config.campaign.verification, "api_provider")
    assert not hasattr(config.campaign.verification, "catch_all_daily_share")


def test_scope_down_removed_the_pool_and_breaker_knobs(config: Config):
    for gone in ("warmup", "circuit_breaker", "daily_global_cap", "step1_variant",
                 "links_base_url"):
        assert not hasattr(config.campaign, gone), f"{gone} should have been removed"
    assert not hasattr(config.campaign.sending_window, "respect_recipient_timezone")
    assert config.campaign.daily_cap == 25
