SHELL := /bin/bash

IMAGE ?= skillbench-session-collector:local

# Enable interactive flags only when a TTY exists.
DOCKER_INTERACTIVE := $(shell if [ -t 0 ] && [ -t 1 ]; then echo "-it"; elif [ -t 0 ]; then echo "-i"; else echo ""; fi)

# Host -> container home mapping for agent log mounts
CONTAINER_HOME ?= /home/app

# Safety: by default, keep the privacy model (only public + OSS auto-included).
# Set INCLUDE_EXCLUDED=1 to opt-in to exporting excluded workspaces too.
INCLUDE_EXCLUDED ?= 0
# YES=1 skips all interactive confirmation prompts (including the private-repo
# manual selection prompt). Use with care — without it, and with no auto-included
# workspaces, you'll get a prompt inside the container.
YES ?= 0
COLLECT_FLAGS :=
ifeq ($(YES),1)
COLLECT_FLAGS += -y
endif
ifeq ($(INCLUDE_EXCLUDED),1)
COLLECT_FLAGS += --include-excluded
endif
ifdef ALLOWED_ORGS
COLLECT_FLAGS += --allowed-orgs $(foreach org,$(ALLOWED_ORGS),"$(org)")
endif

# Fixed mounts for agent session stores (read-only)
AGENT_MOUNTS := \
	-v "$(HOME)/.claude:$(CONTAINER_HOME)/.claude:ro" \
	-v "$(HOME)/.gemini:$(CONTAINER_HOME)/.gemini:ro" \
	-v "$(HOME)/.codex:$(CONTAINER_HOME)/.codex:ro" \
	-v "$(HOME)/.codex-cli:$(CONTAINER_HOME)/.codex-cli:ro" \
	-v "$(HOME)/.openai-codex:$(CONTAINER_HOME)/.openai-codex:ro"

# Optional mounts for gh auth (best-effort; if you use GH_TOKEN env var, you can skip this)
GH_MOUNTS := \
	-v "$(HOME)/.config/gh:$(CONTAINER_HOME)/.config/gh:ro"

# Dynamically computed mounts for real workspace folders referenced by sessions
WORKSPACE_MOUNTS := $(shell python3 scripts/skillbench_docker_mounts.py 2>/dev/null)

.PHONY: docker-build docker-collect docker-collect-all docker-collect-verbose docker-shell preflight-gh

# Preflight: require GitHub CLI auth on the host before starting Docker.
#
# We mount ~/.config/gh into the container read-only; without it, `gh` inside
# the container can't check repo visibility/license, and every workspace falls
# back to "private, no license" — which silently blocks auto-include and
# confuses new users (see Andela pilot feedback, 2026-04).
#
# Policy: HARD-FAIL by default so the user must install/auth gh (or provide a
# token). Advanced users who want to rely on manual private-repo selection can
# bypass with ALLOW_NO_GH=1.
preflight-gh:
	@if [ -n "$$GH_TOKEN" ] || [ -n "$$GITHUB_TOKEN" ]; then \
		echo "✓  Using GH_TOKEN for GitHub classification (docs/gh-token.md)"; \
	elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then \
		echo "✓  gh authenticated as $$(gh api user --jq .login 2>/dev/null || echo unknown)"; \
	elif [ "$${ALLOW_NO_GH:-0}" = "1" ]; then \
		printf '\033[33m%s\033[0m\n' "⚠  ALLOW_NO_GH=1 — continuing without gh. Auto-include will be 0."; \
	else \
		BAR="────────────────────────────────────────────────────────────"; \
		if ! command -v gh >/dev/null 2>&1; then \
			HEAD="✗  gh not installed. Install:"; \
		else \
			HEAD="✗  gh not authenticated. Run:"; \
		fi; \
		printf '\033[31m%s\n%s\033[0m\n' "$$BAR" "$$HEAD"; \
		printf '\033[31m%s\033[0m\n' "     brew install gh && gh auth login       (macOS)"; \
		printf '\033[31m%s\033[0m\n' "     sudo apt install gh && gh auth login   (Debian/Ubuntu)"; \
		printf '\033[31m%s\033[0m\n' "   Or skip install by setting GH_TOKEN — see docs/gh-token.md"; \
		printf '\033[31m%s\033[0m\n' "   Or: ALLOW_NO_GH=1 make docker-collect   (manual selection only)"; \
		printf '\033[31m%s\033[0m\n' "$$BAR"; \
		exit 1; \
	fi

docker-build:
	docker build -t "$(IMAGE)" .

docker-collect: preflight-gh docker-build
	@mkdir -p dist
	docker run --rm $(DOCKER_INTERACTIVE) \
		--user "$$(id -u):$$(id -g)" \
		-e HOME="$(CONTAINER_HOME)" \
		-e PYTHONUNBUFFERED=1 \
		$${GH_TOKEN:+-e GH_TOKEN} \
		$${GITHUB_TOKEN:+-e GITHUB_TOKEN} \
		-v "$$(pwd):/work" -w /work \
		$(AGENT_MOUNTS) \
		$(GH_MOUNTS) \
		$(WORKSPACE_MOUNTS) \
		"$(IMAGE)" \
		python3 skillbench.py collect $(COLLECT_FLAGS)

docker-collect-all:
	$(MAKE) docker-collect INCLUDE_EXCLUDED=1

docker-collect-verbose: docker-build
	@mkdir -p dist
	docker run --rm $(DOCKER_INTERACTIVE) \
		--user "$$(id -u):$$(id -g)" \
		-e HOME="$(CONTAINER_HOME)" \
		-e PYTHONUNBUFFERED=1 \
		$${GH_TOKEN:+-e GH_TOKEN} \
		$${GITHUB_TOKEN:+-e GITHUB_TOKEN} \
		-v "$$(pwd):/work" -w /work \
		$(AGENT_MOUNTS) \
		$(GH_MOUNTS) \
		$(WORKSPACE_MOUNTS) \
		"$(IMAGE)" \
		python3 skillbench.py collect

docker-shell: docker-build
	docker run --rm $(DOCKER_INTERACTIVE) \
		--user "$$(id -u):$$(id -g)" \
		-e HOME="$(CONTAINER_HOME)" \
		-v "$$(pwd):/work" -w /work \
		$(AGENT_MOUNTS) \
		$(GH_MOUNTS) \
		$(WORKSPACE_MOUNTS) \
		"$(IMAGE)" \
		sh

