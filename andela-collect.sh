#!/usr/bin/env bash
set -e

# SkillBench session-collector — Andela pilot
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/andela-collect.sh | bash
#   bash andela-collect.sh [skillbench collect options]
#
# This script installs skillbench via pipx (so `skillbench` is on PATH without
# venv activate) and runs `skillbench collect --allowed-orgs <Andela pilot orgs>`.

REPO_URL="https://github.com/SkillBench-AI/session-collector.git"
REPO_DIR="session-collector"
MIN_PYTHON_MINOR=9
ANDELA_ORGS=(andela-technology woven-teams woven-reviews)

# --- Formatting helpers (match Makefile preflight style) -----------------
C_RED=$'\033[31m'
C_GREEN=$'\033[32m'
C_RESET=$'\033[0m'
BAR="────────────────────────────────────────────────────────────"

ok()   { printf "%s✓  %s%s\n" "$C_GREEN" "$1" "$C_RESET"; }
step() { printf "→  %s\n" "$1"; }

# fail "headline" "hint line 1" "hint line 2" ...  → red boxed block, exit 1
fail() {
    local headline="$1"; shift
    printf "%s%s%s\n" "$C_RED" "$BAR" "$C_RESET" >&2
    printf "%s✗  %s%s\n" "$C_RED" "$headline" "$C_RESET" >&2
    for line in "$@"; do
        printf "%s   %s%s\n" "$C_RED" "$line" "$C_RESET" >&2
    done
    printf "%s%s%s\n" "$C_RED" "$BAR" "$C_RESET" >&2
    exit 1
}

echo "============================================================"
echo "  SkillBench session-collector (Andela pilot)"
echo "============================================================"
echo ""

# ----------------------------------------------------------------------------
# 0. Ensure we're inside a cloned checkout
# ----------------------------------------------------------------------------

is_session_collector_repo() {
    # Check for the migrated src-layout entry point. Earlier versions kept
    # skillbench.py at the repo root; after the packaging refactor it moved to
    # src/skillbench/__init__.py. We only check the new location — the old
    # one no longer exists on current main.
    [ -f "pyproject.toml" ] && [ -f "src/skillbench/__init__.py" ]
}

if ! is_session_collector_repo; then
    if ! command -v git &>/dev/null; then
        fail "git not found." \
            "Install:" \
            "  macOS:         brew install git" \
            "  Debian/Ubuntu: sudo apt install git"
    fi
    if [ -d "$REPO_DIR/.git" ]; then
        step "Repository already cloned. Updating..."
        git -C "$REPO_DIR" pull --ff-only || true
    elif [ -d "$REPO_DIR" ]; then
        fail "'$REPO_DIR' exists but is not a git repository." \
            "Remove it or choose a different working directory."
    else
        step "Cloning SkillBench session-collector..."
        git clone --depth 1 "$REPO_URL" "$REPO_DIR"
    fi
    cd "$REPO_DIR"
    ok "Repository ready: $(pwd)"
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
    fail "Python 3.9+ not found." \
        "Install:" \
        "  macOS:         brew install python" \
        "  Debian/Ubuntu: sudo apt install python3"
fi

PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Python $PYTHON_VERSION found ($PYTHON)"

# ----------------------------------------------------------------------------
# 2. Require pipx
# ----------------------------------------------------------------------------

if ! command -v pipx &>/dev/null; then
    fail "pipx not found." \
        "Install pipx (one-time setup):" \
        "  macOS:         brew install pipx && pipx ensurepath" \
        "  Debian/Ubuntu: sudo apt install pipx && pipx ensurepath" \
        "  Any Python:    $PYTHON -m pip install --user pipx && $PYTHON -m pipx ensurepath" \
        "" \
        "Open a new terminal after \`pipx ensurepath\`, then re-run this script."
fi

ok "pipx found"

# ----------------------------------------------------------------------------
# 3. Install / upgrade skillbench via pipx
# ----------------------------------------------------------------------------

step "Installing skillbench via pipx..."
if pipx list --short 2>/dev/null | awk '{print $1}' | grep -qx skillbench; then
    pipx install --force --python "$PYTHON" "$REPO_ROOT" >/dev/null
else
    pipx install --python "$PYTHON" "$REPO_ROOT" >/dev/null
fi

SKILLBENCH_BIN="$(command -v skillbench || true)"
if [ -z "$SKILLBENCH_BIN" ]; then
    CANDIDATE="$HOME/.local/bin/skillbench"
    [ -x "$CANDIDATE" ] && SKILLBENCH_BIN="$CANDIDATE"
fi

if [ -z "$SKILLBENCH_BIN" ]; then
    fail "Could not locate the installed \`skillbench\` binary." \
        "Try: pipx ensurepath && open a new terminal."
fi

ok "skillbench installed: $SKILLBENCH_BIN"

# ----------------------------------------------------------------------------
# 4. Optional: gh CLI (used for repo classification)
# ----------------------------------------------------------------------------

if command -v gh &>/dev/null; then
    if gh auth status &>/dev/null; then
        GH_USER=$(gh api user --jq .login 2>/dev/null || echo "unknown")
        ok "GitHub CLI authenticated (${GH_USER})"
    else
        step "Authenticating GitHub CLI..."
        gh auth login || true
    fi
else
    printf "\033[33m⚠  gh not found — repo classification will be limited.\033[0m\n"
    printf "\033[33m   Install:\033[0m\n"
    printf "\033[33m     macOS:         brew install gh\033[0m\n"
    printf "\033[33m     Debian/Ubuntu: sudo apt install gh\033[0m\n"
fi

# ----------------------------------------------------------------------------
# 5. Run skillbench collect with Andela pilot org allowlist
# ----------------------------------------------------------------------------

echo ""
echo "============================================================"
echo "  Running skillbench collect (Andela pilot)..."
echo "============================================================"
echo ""

# Wrapped in if/else so `set -e` at the top of the script doesn't abort us
# when collect exits non-zero (e.g. user cancels at the selection prompt,
# or headless EOF with exit 2). We still want the "Done" footer below to
# run with the re-run guidance.
if "$SKILLBENCH_BIN" collect --allowed-orgs "${ANDELA_ORGS[@]}" "$@"; then
    COLLECT_RC=0
else
    COLLECT_RC=$?
fi

# ----------------------------------------------------------------------------
# 6. Re-run guidance
# ----------------------------------------------------------------------------

ANDELA_ORGS_STR="${ANDELA_ORGS[*]}"

echo ""
echo "============================================================"
echo "  Done."
echo "============================================================"
echo ""
echo "  Installed command : $SKILLBENCH_BIN"
echo "  Repo checkout     : $REPO_ROOT"
echo ""
echo "  To re-run later (exactly as just now):"
echo "      $SKILLBENCH_BIN collect --allowed-orgs $ANDELA_ORGS_STR" "$@"
echo ""
if ! command -v skillbench &>/dev/null; then
    echo "  Hint: \`skillbench\` is not on PATH in this shell yet."
    echo "        Run \`pipx ensurepath\` once and open a new terminal,"
    echo "        after which you can just run: skillbench collect --allowed-orgs $ANDELA_ORGS_STR"
    echo ""
fi

exit "$COLLECT_RC"
