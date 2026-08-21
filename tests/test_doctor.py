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


def test_path_failure_names_the_shell_file_and_the_line(monkeypatch, tmp_path):
    """'command not found' is the first thing a new user hits, and the fix is a
    line in a file they have to be told the name of."""
    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(D, "PROFILES", {"zsh": [str(tmp_path / "zshrc")]})
    check = D.check_path()
    assert check.status == D.FAIL
    assert "shell=zsh" in check.detail
    assert any("export PATH" in f for f in check.fix)


def test_path_already_configured_is_a_different_message(monkeypatch, tmp_path):
    """Being told to apply a fix you already applied is worse than no message.
    A profile that adds the path, in a shell that did not read it, is a
    different problem from never having added it -- and it is the common one,
    because installers and editor terminals run non-login shells."""
    from pathlib import Path as _P

    profile = tmp_path / "zshrc"
    profile.write_text(f'export PATH="{_P(D.sys.executable).parent}:$PATH"\n')
    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(D, "PROFILES", {"zsh": [str(profile)]})

    check = D.check_path()
    assert check.status == D.WARN, "an applied fix must not be reported as missing"
    assert "already adds it" in check.detail
    assert "did not read that file" in check.detail
    assert any("open a new interactive terminal" in f for f in check.fix)
    assert not any("echo 'export PATH" in f for f in check.fix), \
        "must not tell them to add a line that is already there"


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


# ----------------------------------------------------------------- installer


def _install_sh() -> str:
    return (Path(__file__).resolve().parent.parent / "install.sh").read_text()


def test_installer_never_overwrites_config_or_state():
    """A club member re-cloning or pulling an update must not lose their
    persona, their contacts, or a suppression list of people who asked not to be
    emailed. That is the one mistake here that cannot be undone, so it is not
    offered, not prompted for, and not reachable by accident."""
    src = _install_sh()
    assert 'if [ -d config ]; then' in src
    assert 'if [ -d state ]; then' in src
    # No copy into an existing config/, and no destructive verbs anywhere.
    for danger in ("rm -rf config", "rm -rf state", "cp -R config.example config\n"
                                                    "  ok"):
        assert danger not in src.replace("  cp -R config.example config", "X"), danger
    assert "left untouched" in src


def test_installer_stops_at_the_first_error():
    """Someone installing this has not used a venv and will not read past the
    first failure, so continuing to collect five problems buries the one that
    matters."""
    src = _install_sh()
    assert "set -euo pipefail" in src
    assert src.count("die ") >= 6, "failures should stop with an explanation"
    assert "Fix that, then run ./install.sh again" in src


def test_installer_runs_doctor_last_and_shows_it():
    """Finishing the installer is the moment someone learns the command exists."""
    src = _install_sh()
    doctor_at = src.index("outbound\" doctor")
    assert doctor_at > src.index("database migrated"), "doctor must run last"
    assert "DOCTOR=$?" in src, "the exit code has to be inspected"
    assert 'if [ "$DOCTOR" -eq 0 ]' in src


def test_installer_handles_a_venv_without_pip():
    """uv does not install pip into the venvs it creates. Assuming `python -m
    pip` exists made the installer fail on its first dependency against a venv
    this project's own developer had."""
    src = _install_sh()
    assert "command -v uv" in src
    assert "uv pip install" in src
    assert "has no pip, and uv is not installed either" in src
