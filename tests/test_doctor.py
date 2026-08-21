"""The command someone runs when nothing works.

Each check here corresponds to a failure this project actually hit. A check that
only proves YAML parses would have caught none of them: the app password was
wrong once, the GitHub token was missing once, dnspython silently degraded every
address to unknown, a Drive link sat behind a request-access wall, and a copy
edit invalidated the test-send gate.

Every failing check must carry a fix. A diagnosis without one relocates the
confusion rather than ending it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import doctor as D


def test_every_failure_carries_a_fix(config_root, monkeypatch):
    """The property that makes this command worth running."""
    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    for check in D.run(config_root):
        if check.status == D.FAIL:
            assert check.fix, f"{check.name} fails without telling anyone what to do"
            assert check.detail, f"{check.name} fails without saying what is wrong"


def test_missing_dnspython_is_reported_as_a_failure(monkeypatch):
    """The silent one: without it every address verifies as unknown and nothing
    errors, so the operator sees a clean run that found no valid addresses."""
    import builtins

    real = builtins.__import__

    def no_dns(name, *a, **k):
        if name.startswith("dns"):
            raise ImportError("no dns")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_dns)
    check = D.check_dnspython()
    assert check.status == D.FAIL
    assert "unknown" in check.detail
    assert any("pip install dnspython" in f for f in check.fix)


def test_path_failure_names_the_shell_file_and_the_line(monkeypatch):
    """'command not found' is the first thing a new user hits, and the fix is a
    line in a file they have to be told the name of."""
    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    check = D.check_path()
    assert check.status == D.FAIL
    assert any("~/.zshrc" in f for f in check.fix)
    assert any("export PATH" in f for f in check.fix)


def test_a_missing_config_says_how_to_scaffold_one(tmp_path):
    check = D.check_config(tmp_path / "nope")
    assert check.status == D.FAIL
    assert any("cp -r config.example config" in f for f in check.fix)


def test_a_wrong_app_password_explains_2fa(config_root, monkeypatch):
    """A 535 is indistinguishable from a typo unless you know app passwords
    require 2-Step Verification to exist at all."""
    from scripts import providers

    class Bad:
        def verify_auth(self):
            return False, "535 5.7.8 Username and Password not accepted"

    monkeypatch.setattr(providers, "build", lambda mb, secrets: Bad())
    check = D.check_mailbox(config_root)
    assert check.status == D.FAIL
    assert any("2-Step Verification" in f for f in check.fix)


def test_secrets_check_never_prints_a_value(config_root):
    """Terminal output lands in transcripts. Presence only, never values."""
    (config_root / "secrets.env").write_text(
        "GMAIL_APP_PASSWORD_PERSONAL=hunter2supersecret\nGITHUB_TOKEN=ghp_realtoken\n")
    check = D.check_secrets(config_root)
    blob = check.detail + " ".join(check.fix)
    assert "hunter2supersecret" not in blob
    assert "ghp_realtoken" not in blob


def test_a_missing_github_token_is_a_warning_not_a_failure(config_root):
    """It degrades one channel rather than stopping the tool, so it must not
    block install.sh."""
    (config_root / "secrets.env").write_text(
        "GMAIL_APP_PASSWORD_PERSONAL=x\nGITHUB_TOKEN=\n")
    check = D.check_secrets(config_root)
    assert check.status == D.WARN
    assert any("NO scopes" in f for f in check.fix)


def test_doctor_exits_non_zero_when_something_fails(config_root, monkeypatch):
    """install.sh gates on the exit code."""
    from typer.testing import CliRunner

    from scripts.outbound import app

    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    result = CliRunner().invoke(app, ["doctor", "--config", str(config_root)])
    assert result.exit_code == 1


def test_config_failure_stops_the_run_early(tmp_path):
    """Nothing below config can run without it, and five cascading errors read
    as five problems rather than one."""
    checks = D.run(tmp_path / "missing")
    assert checks[-1].name == "config loads"
    assert len(checks) == 3
