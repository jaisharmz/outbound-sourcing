#!/usr/bin/env bash
#
# Install outbound-sourcing.
#
# Two rules shape this script.
#
# It stops at the first error. Someone installing this has not used a venv
# before and will not read past the first thing that goes wrong, so continuing
# in order to report five problems at the end just buries the one that matters.
#
# It never touches an existing config/ or state/. Those hold a persona, real
# contacts, a suppression list of people who asked not to be emailed, and
# drafts. Overwriting them is the one mistake here that cannot be undone, so it
# is not offered, not prompted for, and not possible by accident.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
info() { printf '        %s\n' "$1"; }
skip() { printf '  \033[33mkept\033[0m  %s\n' "$1"; }

die() {
  printf '\n\033[31mInstall stopped.\033[0m %s\n\n' "$1"
  shift
  for line in "$@"; do printf '    %s\n' "$line"; done
  printf '\nFix that, then run ./install.sh again. It is safe to re-run.\n\n'
  exit 1
}

bold "outbound-sourcing installer"
printf '  %s\n\n' "$ROOT"

# ---------------------------------------------------------------- python
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v="$($c -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    if [ "$(printf '%s\n3.11\n' "$v" | sort -V | head -1)" = "3.11" ]; then PY="$c"; break; fi
  fi
done
[ -n "$PY" ] || die "No Python 3.11 or newer found." \
  "macOS:  brew install python@3.13" \
  "Debian: sudo apt install python3.13 python3.13-venv" \
  "Then run ./install.sh again."
ok "python: $PY ($($PY -c 'import sys; print(sys.version.split()[0])'))"

# ---------------------------------------------------------------- venv
# uv is used when present: it is much faster, and it is what this project was
# developed with. It does not install pip into the venvs it creates, so the
# installer must not assume `python -m pip` exists -- reusing a uv-made venv
# with a pip-only installer fails on the very first dependency.
USE_UV=""
command -v uv >/dev/null 2>&1 && USE_UV=1

if [ -d .venv ]; then
  ok "virtualenv: reusing .venv"
else
  if [ -n "$USE_UV" ]; then
    uv venv .venv >/dev/null 2>&1 || die "uv could not create the virtualenv." \
      "Try without it:  rm -rf .venv && $PY -m venv .venv && ./install.sh"
  else
    "$PY" -m venv .venv 2>/dev/null || die "Could not create the virtualenv." \
      "Debian/Ubuntu needs the venv package separately:" \
      "  sudo apt install python3-venv"
  fi
  ok "virtualenv: created .venv"
fi
VENV_PY="$ROOT/.venv/bin/python"
[ -x "$VENV_PY" ] || die "The virtualenv exists but has no python in it." \
  "Delete it and re-run:  rm -rf .venv && ./install.sh"

# ---------------------------------------------------------------- deps
printf '  ...   installing dependencies\n'
HAVE_PIP=""
"$VENV_PY" -m pip --version >/dev/null 2>&1 && HAVE_PIP=1

if [ -n "$USE_UV" ]; then
  if ! VIRTUAL_ENV="$ROOT/.venv" uv pip install --quiet -e . >/dev/null 2>&1; then
    printf '\n'; VIRTUAL_ENV="$ROOT/.venv" uv pip install -e . 2>&1 | tail -15
    die "Dependency install failed. The output above says why." \
      "The usual cause is no network, or a proxy that blocks PyPI."
  fi
  ok "dependencies installed (uv)"
elif [ -n "$HAVE_PIP" ]; then
  "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  if ! "$VENV_PY" -m pip install --quiet -e . >/dev/null 2>&1; then
    printf '\n'; "$VENV_PY" -m pip install -e . 2>&1 | tail -15
    die "Dependency install failed. The pip output above says why." \
      "The usual cause is no network, or a proxy that blocks PyPI."
  fi
  ok "dependencies installed (pip)"
else
  die "This virtualenv has no pip, and uv is not installed either." \
    "Either install uv:      curl -LsSf https://astral.sh/uv/install.sh | sh" \
    "or rebuild the venv:    rm -rf .venv && ./install.sh"
fi

"$VENV_PY" -c 'import dns.resolver' 2>/dev/null \
  || die "dnspython did not install, and without it no address can be verified." \
       "This one fails quietly later, so the installer stops here instead." \
       "Try:  $VENV_PY -m pip install dnspython"
ok "dnspython present (address verification will work)"

# ---------------------------------------------------------------- config
if [ -d config ]; then
  skip "config/ already exists -- left untouched"
  info "your persona, ICP and secrets are unchanged"
else
  [ -d config.example ] || die "config.example/ is missing from this checkout." \
    "Re-clone the repository."
  cp -R config.example config
  ok "config/ scaffolded from config.example/"
  info "edit config/persona.md and config/secrets.env before the first run"
fi

if [ -d state ]; then
  skip "state/ already exists -- left untouched"
  info "your contacts, drafts and suppression list are unchanged"
else
  mkdir -p state
  ok "state/ created"
fi

# ---------------------------------------------------------------- database
if ! "$VENV_PY" -m scripts.db >/dev/null 2>&1; then
  printf '\n'; "$VENV_PY" -m scripts.db 2>&1 | tail -8
  die "Database migration failed. The output above says why."
fi
ok "database migrated"

# ---------------------------------------------------------------- skill links
SKILLS="$HOME/.claude/skills"
COMMANDS="$HOME/.claude/commands"
mkdir -p "$SKILLS" "$COMMANDS"

link() {   # link <target> <linkname>  -- idempotent, never clobbers a real dir
  local target="$1" name="$2"
  if [ -L "$name" ]; then
    ln -sfn "$target" "$name"; return 0
  fi
  if [ -e "$name" ]; then
    printf '  \033[33mkept\033[0m  %s exists and is not a symlink -- left alone\n' "$name"
    return 0
  fi
  ln -s "$target" "$name"
}

if [ "$ROOT" != "$SKILLS/outbound-sourcing" ]; then
  link "$ROOT" "$SKILLS/outbound-sourcing"
  ok "skill linked into ~/.claude/skills/"
else
  ok "skill already lives in ~/.claude/skills/"
fi

if [ -f "$ROOT/.claude/commands/outbound.md" ]; then
  link "$ROOT/.claude/commands/outbound.md" "$COMMANDS/outbound.md"
  ok "/outbound command linked into ~/.claude/commands/"
fi

# ---------------------------------------------------------------- PATH
BIN="$ROOT/.venv/bin"
case "$(basename "${SHELL:-}")" in
  zsh)  PROFILE="$HOME/.zshrc" ;;
  bash) PROFILE="$HOME/.bash_profile"; [ -f "$HOME/.bashrc" ] && PROFILE="$HOME/.bashrc" ;;
  *)    PROFILE="$HOME/.profile" ;;
esac
LINE="export PATH=\"$BIN:\$PATH\""
if command -v outbound >/dev/null 2>&1; then
  ok "outbound is on your PATH"
elif [ -f "$PROFILE" ] && grep -qF "$BIN" "$PROFILE"; then
  ok "PATH line already in $PROFILE"
  info "this shell has not read it yet -- open a new terminal"
else
  printf '%s\n' "$LINE" >> "$PROFILE"
  ok "added the PATH line to $PROFILE"
  info "open a new terminal, or run: source $PROFILE"
fi

# ---------------------------------------------------------------- doctor
printf '\n'
bold "Checking the install"
set +e
PATH="$BIN:$PATH" "$BIN/outbound" doctor
DOCTOR=$?
set -e

printf '\n'
if [ "$DOCTOR" -eq 0 ]; then
  bold "Ready."
  cat <<'NEXT'
    Try it:
      outbound investigate "Baseten" --domain baseten.co
      outbound review export --out review.md
      outbound send                       # writes Gmail drafts; never sends

    SETUP.md walks through credentials and the email template.
    USAGE.md is the three-command version.
NEXT
else
  bold "Installed, with setup left to do."
  cat <<'NEXT'
    The checks above list what is missing and the exact fix for each.
    Work down the list, then run:
      outbound doctor

    SETUP.md explains each credential in order.
NEXT
fi
exit 0
