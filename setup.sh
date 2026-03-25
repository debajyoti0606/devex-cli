#!/usr/bin/env bash
set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}${BOLD}▶${RESET} $*"; }
success() { echo -e "${GREEN}${BOLD}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}${BOLD}!${RESET} $*"; }
die()     { echo -e "${RED}${BOLD}✗${RESET} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\n${BOLD}devex — one-time setup${RESET}\n"

# ── 1. Python ≥ 3.10 ─────────────────────────────────────────────────────────
info "Checking Python version..."
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null \
  || die "Python not found. Install Python 3.10+ from https://python.org")

PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ) ]]; then
  die "Python 3.10+ required (found $PY_VER). Please upgrade."
fi
success "Python $PY_VER"

# ── 2. Install devex into user Python ────────────────────────────────────────
# --user installs into ~/.local/  (Linux) or ~/Library/Python/X.Y/  (macOS)
# --editable means changes to source are picked up without reinstalling
info "Installing devex (pip install --user -e .) ..."
"$PYTHON" -m pip install --user -q -e "$SCRIPT_DIR"
success "devex installed"

# ── 3. Ensure the user bin directory is on PATH ───────────────────────────────
USER_BIN=$("$PYTHON" -m site --user-base)/bin

if ! command -v devex &>/dev/null; then
  warn "devex is not on your PATH yet."
  echo -e "  Add this line to your shell profile (~/.zshrc, ~/.bashrc, etc.):"
  echo ""
  echo -e "    ${CYAN}export PATH=\"${USER_BIN}:\$PATH\"${RESET}"
  echo ""
  echo -e "  Then reload your shell:"
  echo -e "    ${CYAN}source ~/.zshrc${RESET}   (or open a new terminal)"

  # Auto-append to the detected shell profile if the user agrees
  SHELL_PROFILE=""
  if [[ -n "${ZSH_VERSION:-}" || "$SHELL" == */zsh ]]; then
    SHELL_PROFILE="$HOME/.zshrc"
  elif [[ -n "${BASH_VERSION:-}" || "$SHELL" == */bash ]]; then
    SHELL_PROFILE="$HOME/.bashrc"
  fi

  if [[ -n "$SHELL_PROFILE" ]]; then
    echo ""
    read -r -p "  Add it to $SHELL_PROFILE automatically? [Y/n] " ans
    ans="${ans:-y}"
    if [[ "${ans,,}" == "y" ]]; then
      echo "" >> "$SHELL_PROFILE"
      echo "# devex CLI" >> "$SHELL_PROFILE"
      echo "export PATH=\"${USER_BIN}:\$PATH\"" >> "$SHELL_PROFILE"
      success "Added to $SHELL_PROFILE"
      warn "Run: source $SHELL_PROFILE   (or open a new terminal)"
    fi
  fi
else
  DEVEX_PATH=$(command -v devex)
  success "devex is on PATH  →  $DEVEX_PATH"
fi

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
echo -e "  Run devex from any project directory:"
echo -e "    ${CYAN}cd /your/project && devex scan${RESET}"
echo ""
echo -e "  To update devex after pulling new changes:"
echo -e "    ${CYAN}cd $(pwd) && pip install --user -e .${RESET}"
echo ""
