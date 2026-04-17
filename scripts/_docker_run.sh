#!/usr/bin/env bash
#
# Internal helper invoked by the Makefile's docker-collect target.
# Not for direct user invocation — reads all configuration from env vars
# exported by the Makefile.
#
# Env inputs:
#   IMAGE            docker image tag (required)
#   CONTAINER_HOME   HOME inside the container (required)
#   YES              "1" → force -y; "0" (default) → TTY-detected
#   INCLUDE_EXCLUDED "1" → pass --include-excluded
#   ALLOWED_ORGS     space-separated list of GitHub orgs (optional)
#   AGENT_MOUNTS     pre-built docker -v flags string for agent log dirs
#   GH_MOUNTS        pre-built docker -v flags string for ~/.config/gh
#   WORKSPACE_MOUNTS pre-built docker -v flags string for session workspaces
#   GH_TOKEN / GITHUB_TOKEN  forwarded to container if set
#   SKILLBENCH_DEBUG "1" → emit debug trace lines to stderr
#
# Responsibilities:
#   1. Auto-inject a GH token from host `gh` when the env has none but the
#      host is authenticated. Avoids reliance on the ~/.config/gh bind mount,
#      which is flaky on macOS Docker Desktop (UID mapping + 0600 files).
#   2. Forward GH_TOKEN / GITHUB_TOKEN / SKILLBENCH_DEBUG into the container
#      by NAME (values never appear in the process list).
#   3. Invoke `docker run` with a TTY flag that reflects the real shell.

set -eu

: "${IMAGE:?IMAGE env var required}"
: "${CONTAINER_HOME:?CONTAINER_HOME env var required}"

YES="${YES:-0}"
INCLUDE_EXCLUDED="${INCLUDE_EXCLUDED:-0}"
ALLOWED_ORGS="${ALLOWED_ORGS:-}"
AGENT_MOUNTS="${AGENT_MOUNTS:-}"
GH_MOUNTS="${GH_MOUNTS:-}"
WORKSPACE_MOUNTS="${WORKSPACE_MOUNTS:-}"

debug() {
    [ -n "${SKILLBENCH_DEBUG:-}" ] && echo "[docker-run] $*" >&2 || true
}

# 1. Auto-inject GH_TOKEN from host gh if no explicit token provided.
# Validate with an ISOLATED GH_CONFIG_DIR so the test reflects how a bare
# `GH_TOKEN=<token> gh` call will behave inside the container (which has
# no host keychain access). Without this, a `gho_*` OAuth token can appear
# valid on the host (keychain-assisted) but fail in the container.
if [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ] \
    && command -v gh >/dev/null 2>&1 \
    && gh auth status >/dev/null 2>&1; then
    _TOK="$(gh auth token 2>/dev/null || true)"
    if [ -n "$_TOK" ]; then
        _TOK_PREFIX="${_TOK:0:4}"
        debug "gh auth token returned ${#_TOK} chars, prefix='${_TOK_PREFIX}'"
        _GH_TEST_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t ghtest)"
        if GH_TOKEN="$_TOK" GH_CONFIG_DIR="$_GH_TEST_DIR" gh auth status >/dev/null 2>&1; then
            export GH_TOKEN="$_TOK"
            debug "injected GH_TOKEN from host gh (prefix=${_TOK_PREFIX}, ${#GH_TOKEN} chars)"
        else
            # OAuth session tokens commonly fail standalone validation. Warn
            # the caller prominently since this is the most common pain point.
            printf '\033[33m────────────────────────────────────────────────────────────\033[0m\n'
            printf '\033[33m⚠  Host gh is authenticated, but its token was rejected\033[0m\n'
            printf '\033[33m   standalone (prefix %s, likely an OAuth session token).\033[0m\n' "$_TOK_PREFIX"
            printf '\033[33m   Container gh will not be able to use it.\033[0m\n'
            printf '\033[33m\033[0m\n'
            printf '\033[33m   Fix: use a Personal Access Token instead.\033[0m\n'
            printf '\033[33m     1. Create one: https://github.com/settings/tokens\033[0m\n'
            printf '\033[33m        (Classic, repo scope; if your org uses SSO,\033[0m\n'
            printf '\033[33m        click "Configure SSO" → Authorize)\033[0m\n'
            printf '\033[33m     2. Re-run with it:\033[0m\n'
            printf '\033[33m          GH_TOKEN=ghp_xxxx make docker-collect ...\033[0m\n'
            printf '\033[33m        or re-login non-interactively:\033[0m\n'
            printf '\033[33m          gh auth logout --hostname github.com\033[0m\n'
            printf '\033[33m          echo "ghp_xxxx" | gh auth login --with-token\033[0m\n'
            printf '\033[33m────────────────────────────────────────────────────────────\033[0m\n'
            debug "skipping auto-inject; container will run without GH_TOKEN"
        fi
        rm -rf "$_GH_TEST_DIR" 2>/dev/null || true
    else
        debug "gh auth status succeeded but gh auth token returned empty"
    fi
else
    if [ -n "${GH_TOKEN:-}" ]; then
        debug "GH_TOKEN already set in env"
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        debug "GITHUB_TOKEN already set in env"
    else
        debug "no host gh auth available; container will rely on bind mount (if any)"
    fi
fi

# 2. Build the collect-command args from env toggles.
COLLECT_ARGS=()

if [ "$YES" = "1" ] || ! [ -t 0 ]; then
    # Force non-interactive when user asks OR there's no TTY on stdin.
    COLLECT_ARGS+=("-y")
fi
if [ "$INCLUDE_EXCLUDED" = "1" ]; then
    COLLECT_ARGS+=("--include-excluded")
fi
if [ -n "$ALLOWED_ORGS" ]; then
    COLLECT_ARGS+=("--allowed-orgs")
    # ALLOWED_ORGS is space-separated. read -a word-splits into a bash array
    # without invoking a subshell / eval.
    read -r -a _orgs <<<"$ALLOWED_ORGS"
    COLLECT_ARGS+=("${_orgs[@]}")
fi

debug "collect args: ${COLLECT_ARGS[*]:-<none>}"

# 3. TTY flag for docker — check THIS script's stdin, which is the user's
#    real terminal (unlike make's $(shell ...) subshell whose stdout/stdin
#    are captured).
if [ -t 0 ]; then
    TTY_FLAG="-it"
else
    TTY_FLAG="-i"
fi

# 4. Decide whether to mount ~/.config/gh into the container.
#    - With GH_TOKEN / GITHUB_TOKEN set: DO NOT mount. A bare env-var token
#      is all gh needs, and the host's hosts.yml can otherwise confuse the
#      container gh (e.g. when the host uses OAuth/keychain credentials that
#      only round-trip when the right config scaffolding is present).
#    - Without a token: mount so the container can fall back to the host's
#      existing gh auth.
if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
    EFFECTIVE_GH_MOUNTS=""
    debug "token present → skipping ~/.config/gh bind mount (avoids host/container config conflict)"
else
    EFFECTIVE_GH_MOUNTS="$GH_MOUNTS"
fi

# 5. Run. Mount strings are expanded unquoted so their multiple -v flags
#    word-split correctly. GH_TOKEN/etc. are forwarded by NAME (docker -e VAR
#    without =value picks up the current env value).
# shellcheck disable=SC2086  # intentional word-splitting on mount strings
exec docker run --rm $TTY_FLAG \
    --user "$(id -u):$(id -g)" \
    -e HOME="$CONTAINER_HOME" \
    -e PYTHONUNBUFFERED=1 \
    ${GH_TOKEN:+-e GH_TOKEN} \
    ${GITHUB_TOKEN:+-e GITHUB_TOKEN} \
    ${SKILLBENCH_DEBUG:+-e SKILLBENCH_DEBUG} \
    -v "$(pwd):/work" -w /work \
    $AGENT_MOUNTS \
    $EFFECTIVE_GH_MOUNTS \
    $WORKSPACE_MOUNTS \
    "$IMAGE" \
    python3 -m skillbench collect "${COLLECT_ARGS[@]}"
