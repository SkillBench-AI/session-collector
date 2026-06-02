#!/usr/bin/env bash
set -e

# SkillBench session-collector
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/collect.sh | bash
#   bash collect.sh [skillbench collect options]
#
# This script:
#   1) Installs skillbench via pipx so the `skillbench` command is on PATH globally
#      (no venv activate required)
#   2) Runs `skillbench collect`
#   3) Prints a clear re-run command so you never need to remember the env

PACKAGE_SPEC="${SKILLBENCH_PACKAGE:-skillbench-session-collector}"
MIN_PYTHON_MINOR=9

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
echo "  SkillBench session-collector"
echo "============================================================"
echo ""

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
# 2. Require pipx so `skillbench` ends up on PATH without venv activate.
#    We intentionally do NOT auto-bootstrap pipx here — a missing pipx is
#    usually a signal that the user's Python packaging env is non-standard,
#    and an unattended `pip install --user pipx` in those envs causes more
#    problems than it solves. We give actionable install instructions.
# ----------------------------------------------------------------------------

if ! command -v pipx &>/dev/null; then
    fail "pipx not found." \
        "pipx installs skillbench globally so you don't need to activate a venv." \
        "" \
        "Install:" \
        "  macOS:         brew install pipx && pipx ensurepath" \
        "  Debian/Ubuntu: sudo apt install pipx && pipx ensurepath" \
        "  Any Python:    $PYTHON -m pip install --user pipx && $PYTHON -m pipx ensurepath" \
        "" \
        "Open a new terminal after running \`pipx ensurepath\`, then re-run this script."
fi

ok "pipx found"

# ----------------------------------------------------------------------------
# 3. Check git (needed by skillbench to read remotes)
# ----------------------------------------------------------------------------

if command -v git &>/dev/null; then
    ok "git found"
else
    fail "git not found." \
        "Install:" \
        "  macOS:         brew install git" \
        "  Debian/Ubuntu: sudo apt install git"
fi

# ----------------------------------------------------------------------------
# 4. Install / upgrade skillbench via pipx
# ----------------------------------------------------------------------------

step "Installing skillbench via pipx from: $PACKAGE_SPEC"
pipx install --force --python "$PYTHON" "$PACKAGE_SPEC" >/dev/null

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
    fail "Could not locate the installed \`skillbench\` binary." \
        "Try: pipx ensurepath && open a new terminal."
fi

ok "skillbench installed: $SKILLBENCH_BIN"

# ----------------------------------------------------------------------------
# 5. Optional: gh CLI (used for repo classification)
# ----------------------------------------------------------------------------

if command -v gh &>/dev/null; then
    if gh auth status &>/dev/null; then
        GH_USER=$(gh api user --jq .login 2>/dev/null || echo "unknown")
        ok "GitHub CLI authenticated (${GH_USER})"
    elif [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
        ok "Using GH_TOKEN for GitHub classification (docs/gh-token.md)"
    elif [ -t 1 ] && [ -z "${CI:-}" ]; then
        # Interactive terminal + not CI → run the device-code flow.
        # `gh auth login` opens its own prompts against /dev/tty so it still
        # works when stdin is the curl pipe.
        step "Authenticating GitHub CLI..."
        gh auth login || true
    else
        # Non-TTY (CI, piped stdout, nohup, &) → skip interactive auth so we
        # don't block for the 15-min device-code timeout. skillbench will
        # classify non-publicly as a safe default.
        printf "\033[33m⚠  gh installed but not authenticated, and no terminal to run \`gh auth login\`.\033[0m\n"
        printf "\033[33m   Run \`gh auth login\` manually, or set GH_TOKEN. See docs/gh-token.md.\033[0m\n"
    fi
else
    printf "\033[33m⚠  gh not found. Without it, all repos are classified as private (safe default).\033[0m\n"
    printf "\033[33m   Install for richer repo classification:\033[0m\n"
    printf "\033[33m     macOS:         brew install gh\033[0m\n"
    printf "\033[33m     Debian/Ubuntu: sudo apt install gh\033[0m\n"
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
# Wrapped in if/else so `set -e` at the top of the script doesn't abort us
# when collect exits non-zero (e.g. user cancels at the selection prompt
# with exit 0, or headless EOF with exit 2). We still want the "Done"
# footer below to run with the re-run guidance.
if "$SKILLBENCH_BIN" collect "$@"; then
    COLLECT_RC=0
else
    COLLECT_RC=$?
fi

# ----------------------------------------------------------------------------
# 7. Re-run guidance (works even if ~/.local/bin is not on PATH yet)
# ----------------------------------------------------------------------------

echo ""
echo "============================================================"
echo "  Done."
echo "============================================================"
echo ""
echo "  Installed command : $SKILLBENCH_BIN"
echo "  Package source    : $PACKAGE_SPEC"
echo ""
echo "  To re-run later (exactly as just now):"
# `printf '%q'` escapes each argument so the printed command round-trips
# through shell parsing (preserves spaces, quotes, globs, etc.).
RERUN_CMD="$(printf '%q ' "$SKILLBENCH_BIN" collect "$@")"
echo "      ${RERUN_CMD% }"
echo ""
if ! command -v skillbench &>/dev/null; then
    echo "  Hint: \`skillbench\` is not on PATH in this shell yet."
    echo "        Run \`pipx ensurepath\` once and open a new terminal,"
    echo "        after which \`skillbench collect\` will work globally."
    echo ""
fi

exit "$COLLECT_RC"
