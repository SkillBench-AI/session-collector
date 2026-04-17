#!/usr/bin/env bash
set -e

# SkillBench session-collector
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/scripts/collect.sh | bash
#   bash collect.sh [skillbench collect options]
#
# This script:
#   1) Ensures the session-collector repo is cloned locally (handles `curl | bash`)
#   2) Installs skillbench via pipx so the `skillbench` command is on PATH globally
#      (no venv activate required)
#   3) Runs `skillbench collect`
#   4) Prints a clear re-run command so you never need to remember the env

REPO_URL="https://github.com/SkillBench-AI/session-collector.git"
REPO_DIR="session-collector"
MIN_PYTHON_MINOR=9

echo "============================================================"
echo "  SkillBench session-collector"
echo "============================================================"
echo ""

# ----------------------------------------------------------------------------
# 0. Ensure we're sitting inside a cloned session-collector checkout
# ----------------------------------------------------------------------------

is_session_collector_repo() {
    [ -f "pyproject.toml" ] && [ -f "skillbench.py" ]
}

if ! is_session_collector_repo; then
    if ! command -v git &>/dev/null; then
        echo "❌  git not found. Install it first:"
        echo "      macOS:  brew install git"
        echo "      Linux:  sudo apt install git"
        exit 1
    fi
    if [ -d "$REPO_DIR/.git" ]; then
        echo "→  Repository already cloned. Updating..."
        git -C "$REPO_DIR" pull --ff-only || true
    elif [ -d "$REPO_DIR" ]; then
        echo "❌  '$REPO_DIR' exists but is not a git repository."
        echo "   Remove it or choose a different working directory."
        exit 1
    else
        echo "→  Cloning SkillBench session-collector..."
        git clone --depth 1 "$REPO_URL" "$REPO_DIR"
    fi
    cd "$REPO_DIR"
    echo "✓  Repository ready: $(pwd)"
    echo ""
fi

REPO_ROOT="$(pwd)"

# ----------------------------------------------------------------------------
# 1. Find Python 3.9+
# ----------------------------------------------------------------------------

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
        major=$("$candidate" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
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

# ----------------------------------------------------------------------------
# 2. Require pipx so `skillbench` ends up on PATH without venv activate.
#    We intentionally do NOT auto-bootstrap pipx here — a missing pipx is
#    usually a signal that the user's Python packaging env is non-standard,
#    and an unattended `pip install --user pipx` in those envs causes more
#    problems than it solves. We give actionable install instructions.
# ----------------------------------------------------------------------------

if ! command -v pipx &>/dev/null; then
    echo "❌  pipx not found."
    echo ""
    echo "    pipx installs skillbench globally so you don't need to activate a venv."
    echo ""
    echo "    Install pipx:"
    echo "      macOS:       brew install pipx && pipx ensurepath"
    echo "      Debian/Ubuntu: sudo apt install pipx && pipx ensurepath"
    echo "      Any Python:  $PYTHON -m pip install --user pipx && $PYTHON -m pipx ensurepath"
    echo ""
    echo "    Open a new terminal after running \`pipx ensurepath\`, then re-run this script."
    exit 1
fi

echo "✓  pipx found"

# ----------------------------------------------------------------------------
# 3. Check git (needed by skillbench to read remotes)
# ----------------------------------------------------------------------------

if command -v git &>/dev/null; then
    echo "✓  git found"
else
    echo "❌  git not found."
    echo "      macOS:  brew install git"
    echo "      Linux:  sudo apt install git"
    exit 1
fi

# ----------------------------------------------------------------------------
# 4. Install / upgrade skillbench via pipx
# ----------------------------------------------------------------------------

echo "→  Installing skillbench via pipx..."
if pipx list --short 2>/dev/null | awk '{print $1}' | grep -qx skillbench; then
    # Upgrade an already-installed pipx package in-place from this checkout.
    pipx install --force --python "$PYTHON" "$REPO_ROOT" >/dev/null
else
    pipx install --python "$PYTHON" "$REPO_ROOT" >/dev/null
fi

SKILLBENCH_BIN="$(command -v skillbench || true)"
if [ -z "$SKILLBENCH_BIN" ]; then
    # pipx installs to ~/.local/bin by default; that dir may not be on PATH
    # in the CURRENT shell even after install. Resolve explicitly.
    CANDIDATE="$HOME/.local/bin/skillbench"
    if [ -x "$CANDIDATE" ]; then
        SKILLBENCH_BIN="$CANDIDATE"
    fi
fi

if [ -z "$SKILLBENCH_BIN" ]; then
    echo "❌  Could not locate the installed \`skillbench\` binary."
    echo "    Try: pipx ensurepath && open a new terminal."
    exit 1
fi

echo "✓  skillbench installed: $SKILLBENCH_BIN"

# ----------------------------------------------------------------------------
# 5. Optional: gh CLI (used for repo classification)
# ----------------------------------------------------------------------------

if command -v gh &>/dev/null; then
    if gh auth status &>/dev/null; then
        GH_USER=$(gh api user --jq .login 2>/dev/null || echo "unknown")
        echo "✓  GitHub CLI authenticated (${GH_USER})"
    else
        echo "→  Authenticating GitHub CLI..."
        gh auth login || true
    fi
else
    echo "⚠  gh not found. Install it for richer repo classification (brew install gh / sudo apt install gh)."
    echo "   Without gh, all repos are classified as private (safe default)."
fi

# ----------------------------------------------------------------------------
# 6. Run skillbench collect
# ----------------------------------------------------------------------------

echo ""
echo "============================================================"
echo "  Running skillbench collect..."
echo "============================================================"
echo ""

# Forward any args passed to collect.sh to skillbench collect.
"$SKILLBENCH_BIN" collect "$@"
COLLECT_RC=$?

# ----------------------------------------------------------------------------
# 7. Re-run guidance (works even if ~/.local/bin is not on PATH yet)
# ----------------------------------------------------------------------------

echo ""
echo "============================================================"
echo "  Done."
echo "============================================================"
echo ""
echo "  Installed command : $SKILLBENCH_BIN"
echo "  Repo checkout     : $REPO_ROOT"
echo ""
echo "  To re-run later (exactly as just now):"
echo "      $SKILLBENCH_BIN collect" "$@"
echo ""
if ! command -v skillbench &>/dev/null; then
    echo "  Hint: \`skillbench\` is not on PATH in this shell yet."
    echo "        Run \`pipx ensurepath\` once and open a new terminal,"
    echo "        after which \`skillbench collect\` will work globally."
    echo ""
fi

exit "$COLLECT_RC"
