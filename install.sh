#!/usr/bin/env bash
# SkillBench session-collector one-command installer.
#
# Designed to be run via:
#
#   curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/install.sh | bash
#
# The installer is intentionally conservative: it never sudo's, never
# touches your shell rc files (pipx ensurepath does that, with prompts), and
# installs the published package via pipx so no repository checkout is needed.

set -Eeuo pipefail

PACKAGE_SPEC="${SKILLBENCH_PACKAGE:-skillbench-session-collector}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }

require_or_install() {
    local cmd="$1"
    local hint="$2"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        red "✗  Missing required tool: $cmd"
        red "   $hint"
        exit 1
    fi
}

bold "SkillBench session-collector — one-command install"
echo

require_or_install python3 "Install Python 3.9+ — macOS: brew install python | Debian/Ubuntu: sudo apt install python3"

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
if [[ "$PY_OK" != "1" ]]; then
    red "✗  Python 3.9+ required (found $(python3 -V 2>&1))"
    exit 1
fi

if ! command -v pipx >/dev/null 2>&1; then
    yellow "ℹ  pipx not found — attempting install via 'python3 -m pip install --user pipx'"
    if ! python3 -m pip install --user pipx; then
        red "✗  Could not install pipx automatically."
        red "   macOS:           brew install pipx && pipx ensurepath"
        red "   Debian/Ubuntu:   sudo apt install pipx && pipx ensurepath"
        red "   Any Python:      python3 -m pip install --user pipx && python3 -m pipx ensurepath"
        exit 1
    fi
    if ! command -v pipx >/dev/null 2>&1; then
        # pipx was installed but not yet on PATH; use the module form.
        yellow "ℹ  pipx is installed but not yet on PATH. Continuing via 'python3 -m pipx'."
        yellow "   Run 'python3 -m pipx ensurepath' afterwards to expose it permanently."
        PIPX="python3 -m pipx"
    else
        PIPX="pipx"
    fi
else
    PIPX="pipx"
fi

bold "Installing skillbench via pipx"
echo "  package: $PACKAGE_SPEC"
$PIPX install --force "$PACKAGE_SPEC"

echo
if command -v skillbench >/dev/null 2>&1; then
    green "✓  skillbench installed and on PATH"
    skillbench --help >/dev/null 2>&1 || true
else
    yellow "ℹ  skillbench installed but not yet on PATH."
    yellow "   Run 'pipx ensurepath' (or 'python3 -m pipx ensurepath') and start a new shell."
fi

echo
bold "Next steps"
echo "  1. Verify the install:        skillbench doctor"
echo "  2. Collect Codex sessions:    skillbench codex collect --allowed-orgs <your-org>"
echo "  3. Install the Codex plugin:  skillbench codex plugin-install"
echo
green "Done."
