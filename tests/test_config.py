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
    path.write_text(path.read_text().replace("daily_global_cap:", "dailyglobalcap:"))
    with pytest.raises(ConfigError) as exc:
        Config(config_root)
    msg = str(exc.value)
    assert "unknown key `dailyglobalcap`" in msg
    assert "did you mean `daily_global_cap`?" in msg


def test_unknown_nested_key_names_its_own_level(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text().replace("  start_per_day: 10", "  start_perday: 10"))
    with pytest.raises(ConfigError) as exc:
        Config(config_root)
    msg = str(exc.value)
    assert "unknown key `start_perday`" in msg
    assert "warmup" in msg
    assert "did you mean `start_per_day`?" in msg


def test_missing_template_is_caught_before_any_send(config_root):
    (config_root / "templates" / "step2_bump.md").unlink()
    with pytest.raises(ConfigError, match="missing template"):
        Config(config_root)


def test_missing_attachment_is_caught_before_any_send(config_root):
    cfg = Config(config_root)
    target = next(iter(cfg.sequence.attachment_sets.values()))
    from pathlib import Path
    (Path(cfg.campaign.attachments_root) / target.dir / target.files[0]).unlink()
    with pytest.raises(ConfigError, match="missing file"):
        Config(config_root)


def test_undefined_attachment_set_is_rejected(config_root):
    path = config_root / "sequence.yaml"
    path.write_text(path.read_text().replace("attachment_set: first_touch",
                                             "attachment_set: does_not_exist"))
    with pytest.raises(ConfigError, match="does_not_exist"):
        Config(config_root)


def test_links_variant_requires_a_base_url(config_root):
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text().replace("step1_variant: attachments",
                                             "step1_variant: links"))
    with pytest.raises(ConfigError, match="links_base_url"):
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
    path.write_text(path.read_text().replace("id: outreach-01", "id: console"))
    with pytest.raises(ConfigError, match="duplicate mailbox id"):
        Config(config_root)


def test_mailbox_from_and_reply_to_are_separate(config: Config):
    mb = config.mailboxes.get("outreach-01")
    assert mb.from_.address == "you@sending-domain.com"
    assert mb.reply_to == "you@your-institution.edu"
    assert mb.from_.header() == "Your Name <you@sending-domain.com>"


def test_oversized_attachment_set_hard_fails_with_a_breakdown(config_root):
    """Base64 inflates by 4/3, and many corporate gateways reject above 5-10 MB.
    An oversized set hard-bounces for reasons unrelated to address quality,
    which contaminates bounce rate and can trip the circuit breaker falsely."""
    from pathlib import Path
    import yaml

    cfg = Config(config_root)
    root = Path(cfg.campaign.attachments_root)
    big = root / "_first_email" / "example_document_a.pdf"
    big.write_bytes(b"x" * 8_000_000)

    with pytest.raises(ConfigError) as exc:
        Config(config_root)
    msg = str(exc.value)
    assert "on the wire" in msg
    assert "max_attachment_bytes" in msg
    assert "example_document_a.pdf" in msg
    assert "%" in msg                      # per-file share
    assert "dropping example_document_a.pdf leaves" in msg


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


def test_placeholder_mailing_address_blocks_a_campaign(config: Config):
    """CAN-SPAM needs a real address, and the footer ships on every template."""
    path = config.root / "persona.md"
    path.write_text(path.read_text().replace("123 Example Street", "[STREET ADDRESS NEEDED]"))
    reloaded = Config(config.root)
    blockers = reloaded.preflight("campaign")
    assert any("STREET ADDRESS NEEDED" in b for b in blockers)
    assert any("CAN-SPAM" in b for b in blockers)


def test_placeholder_does_not_stop_the_config_from_loading(config: Config):
    """A test send to yourself must still render the placeholder so you see it."""
    path = config.root / "persona.md"
    path.write_text(path.read_text().replace("123 Example Street", "[STREET ADDRESS NEEDED]"))
    Config(config.root)   # must not raise


def test_clean_config_has_no_blockers(config: Config):
    assert config.preflight("campaign") == []


def test_enabled_mailbox_with_a_placeholder_from_blocks(config_root):
    path = config_root / "mailboxes.yaml"
    text = path.read_text().replace(
        "      address: you@sending-domain.com", "      address: jai@SENDING-DOMAIN-TBD.com"
    ).replace("    enabled: false", "    enabled: true")
    path.write_text(text)
    blockers = Config(config_root).preflight("campaign")
    assert any("placeholder" in b for b in blockers)


def test_disabled_mailbox_placeholder_does_not_block(config_root):
    path = config_root / "mailboxes.yaml"
    path.write_text(path.read_text().replace(
        "      address: you@sending-domain.com", "      address: jai@SENDING-DOMAIN-TBD.com"))
    assert Config(config_root).preflight("campaign") == []


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
    """The whole point of the split: raising one ceiling must not raise the other."""
    _oversize(config_root)
    path = config_root / "campaign.yaml"
    path.write_text(path.read_text() + "\nmax_attachment_bytes: 20000000\n")
    blockers = Config(config_root).preflight("campaign")
    assert any("campaign_max_attachment_bytes" in b for b in blockers)
    assert any("must not ship a set this size to strangers" in b for b in blockers)


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
