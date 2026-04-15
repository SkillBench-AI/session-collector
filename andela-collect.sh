#!/usr/bin/env bash
set -e

# SkillBench session-collector — Andela pilot
# Usage: bash andela-collect.sh [skillbench collect options]
#   e.g. bash andela-collect.sh --split none
#        bash andela-collect.sh --upload-guide

VENV_DIR=".venv"
MIN_PYTHON_MINOR=9

echo "============================================================"
echo "  SkillBench session-collector (Andela pilot)"
echo "============================================================"
echo ""

# --- 1. Find Python 3.9+ ---
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        major=$("$candidate" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        if [ "$major" = "3" ] && [ "$version" -ge "$MIN_PYTHON_MINOR" ] 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌  Python 3.9+ not found."
    echo ""
    echo "    Install it first:"
    echo "      macOS:  brew install python@3.13"
    echo "      Linux:  sudo apt install python3.9"
    echo ""
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
echo "✓  Python $PYTHON_VERSION found ($PYTHON)"

# --- 2. Create venv ---
VENV_PYTHON_BIN="$VENV_DIR/bin/python"
VENV_PIP_BIN="$VENV_DIR/bin/pip"
NEEDS_VENV=0

if [ ! -d "$VENV_DIR" ]; then
    NEEDS_VENV=1
elif ! "$VENV_PYTHON_BIN" --version &>/dev/null; then
    NEEDS_VENV=1
elif ! "$VENV_PIP_BIN" --version &>/dev/null; then
    NEEDS_VENV=1
else
    VENV_PYTHON_VERSION=$("$VENV_PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
    CURRENT_PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [ "$VENV_PYTHON_VERSION" != "$CURRENT_PYTHON_VERSION" ]; then
        NEEDS_VENV=1
    fi
fi

if [ "$NEEDS_VENV" = "1" ]; then
    [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
    echo "→  Creating virtual environment ($VENV_DIR)..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "✓  Virtual environment already exists ($VENV_DIR)"
fi

VENV_PIP="$VENV_DIR/bin/pip"
VENV_SKILLBENCH="$VENV_DIR/bin/skillbench"

# --- 3. Check git ---
if command -v git &>/dev/null; then
    echo "✓  git found"
else
    echo "❌  git not found."
    echo ""
    echo "    Install it first:"
    echo "      macOS:  brew install git"
    echo "      Linux:  sudo apt install git"
    echo ""
    exit 1
fi

# --- 4. Install skillbench ---
echo "→  Installing skillbench..."
"$VENV_PIP" install --quiet -e .
echo "✓  skillbench installed"

# --- 5. Install gh CLI if missing ---
echo ""
if command -v gh &>/dev/null; then
    echo "✓  GitHub CLI already installed"
else
    echo "→  GitHub CLI not found. Installing..."
    OS=$(uname -s)
    if [ "$OS" = "Darwin" ]; then
        if command -v brew &>/dev/null; then
            brew install gh
        else
            echo "⚠  Homebrew not found. Install gh manually: https://cli.github.com"
        fi
    elif [ "$OS" = "Linux" ]; then
        if command -v apt &>/dev/null && command -v dpkg &>/dev/null; then
            # Debian/Ubuntu
            if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo"; fi
            command -v curl &>/dev/null || { $SUDO apt update -qq && $SUDO apt install -y curl; }
            curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                | $SUDO dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
                | $SUDO tee /etc/apt/sources.list.d/github-cli.list > /dev/null
            $SUDO apt update -qq && $SUDO apt install -y gh
        else
            echo "⚠  Auto-install not supported on this Linux distro."
            echo "   Install gh manually: https://cli.github.com"
        fi
    else
        echo "⚠  Unsupported OS. Install gh manually: https://cli.github.com"
    fi
fi

# --- 6. gh auth ---
if command -v gh &>/dev/null; then
    if gh auth status &>/dev/null; then
        GH_USER=$(gh api user --jq .login 2>/dev/null || echo "unknown")
        echo "✓  GitHub CLI authenticated (${GH_USER})"
    else
        echo "→  Authenticating GitHub CLI..."
        gh auth login
    fi
fi

# --- 7. Run skillbench collect (Andela org scope) ---
echo ""
echo "============================================================"
echo "  Running skillbench collect..."
echo "============================================================"
echo ""
"$VENV_SKILLBENCH" collect --allowed-orgs andela-technology woven-teams woven-reviews "$@"
