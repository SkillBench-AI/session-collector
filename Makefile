SHELL := /bin/bash

IMAGE ?= skillbench-session-collector:local

# Enable interactive flags only when a TTY exists.
DOCKER_INTERACTIVE := $(shell if [ -t 0 ] && [ -t 1 ]; then echo "-it"; elif [ -t 0 ]; then echo "-i"; else echo ""; fi)

# Host -> container home mapping for agent log mounts
CONTAINER_HOME ?= /home/app

# Safety: by default, keep the privacy model (only public + OSS auto-included).
# Set INCLUDE_EXCLUDED=1 to opt-in to exporting excluded workspaces too.
INCLUDE_EXCLUDED ?= 0
COLLECT_FLAGS := -y
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

.PHONY: docker-build docker-collect docker-collect-all docker-collect-verbose docker-shell

docker-build:
	docker build -t "$(IMAGE)" .

docker-collect: docker-build
	@mkdir -p dist
	docker run --rm \
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

