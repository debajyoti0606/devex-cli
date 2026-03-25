#!/usr/bin/env bash
set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}${BOLD}▶${RESET} $*"; }
success() { echo -e "${GREEN}${BOLD}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}${BOLD}!${RESET} $*"; }
die()     { echo -e "${RED}${BOLD}✗${RESET} $*" >&2; exit 1; }

echo -e "\n${BOLD}devex — one-time setup${RESET}\n"

# ── 1. Python ≥ 3.10 ─────────────────────────────────────────────────────────
info "Checking Python version..."
PYTHON=$(command -v python3 || command -v python || die "Python not found. Install Python 3.10+.")
PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ) ]]; then
  die "Python 3.10+ required (found $PY_VER). Please upgrade."
fi
success "Python $PY_VER"

# ── 2. pipx ───────────────────────────────────────────────────────────────────
# pipx installs CLI tools in isolated venvs but exposes them globally on PATH.
# This means `devex` works from any terminal without activating anything.
info "Checking for pipx..."
if ! command -v pipx &>/dev/null; then
  warn "pipx not found — installing it now..."
  if command -v brew &>/dev/null; then
    brew install pipx
  else
    "$PYTHON" -m pip install --user pipx
    "$PYTHON" -m pipx ensurepath
    # reload PATH so pipx is usable immediately in this script
    export PATH="$HOME/.local/bin:$PATH"
  fi
  success "pipx installed"
else
  success "pipx $(pipx --version)"
fi

# ── 3. Install devex via pipx (editable) ─────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info "Installing devex globally via pipx ..."
# --editable means changes to the source are picked up without reinstalling
if pipx list --short 2>/dev/null | grep -q "^claude-cli "; then
  info "Already installed — reinstalling to pick up latest changes..."
  pipx reinstall claude-cli --editable 2>/dev/null || pipx install --editable "$SCRIPT_DIR" --force
else
  pipx install --editable "$SCRIPT_DIR"
fi
success "devex installed  →  $(command -v devex)"

# ── 4. .env setup ────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"

if [[ ! -f ".env" ]]; then
  info "Creating .env from .env.example ..."
  cp .env.example .env
fi

API_KEY_VALUE=$(grep -E '^ANTHROPIC_API_KEY=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
if [[ -z "$API_KEY_VALUE" || "$API_KEY_VALUE" == "your-api-key-here" ]]; then
  echo ""
  echo -e "${YELLOW}Your API key is not configured.${RESET}"
  echo -e "  Edit ${BOLD}.env${RESET} and set ${BOLD}ANTHROPIC_API_KEY${RESET}."
  echo -e "  You can also set ${BOLD}ANTHROPIC_BASE_URL${RESET} if you use a proxy endpoint."
  echo ""
  read -r -p "  Paste your ANTHROPIC_API_KEY now (or press Enter to skip): " user_key
  if [[ -n "$user_key" ]]; then
    if sed --version &>/dev/null 2>&1; then
      sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${user_key}|" .env
    else
      sed -i '' "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${user_key}|" .env
    fi
    success "API key saved to .env"
  else
    warn "Skipped — remember to set ANTHROPIC_API_KEY in .env before running devex"
  fi
else
  success "ANTHROPIC_API_KEY already set in .env"
fi

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Setup complete!${RESET}"
echo ""
echo -e "  ${BOLD}devex${RESET} is now available globally — no activation needed."
echo -e "  Run it from any project directory:"
echo -e "    ${CYAN}cd /your/project && devex scan${RESET}"
echo ""
echo -e "  To update devex after pulling new changes:"
echo -e "    ${CYAN}pipx reinstall claude-cli${RESET}"
echo ""
