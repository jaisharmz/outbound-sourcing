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
