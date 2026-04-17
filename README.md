# session-collector

Collect and analyze your AI coding sessions locally. No data leaves your machine until you share the sanitized export.

## Prerequisites

Install once, on the host:

- **Docker** — for the recommended Docker workflow (macOS/Linux/Windows via Docker Desktop).
- **GitHub authentication** — either install the GitHub CLI **or** use a Personal Access Token:
  - `gh` CLI: `brew install gh && gh auth login` (macOS) / `sudo apt install gh && gh auth login` / [cli.github.com](https://cli.github.com/)
  - Or set `GH_TOKEN=<personal access token>` before running (see [docs/gh-token.md](docs/gh-token.md) for token scopes + safe usage)
- **git** — needed to read remotes from your project folders.
- *(Python workflows only)* **Python 3.9+** and **[pipx](https://pipx.pypa.io/)**:
  - macOS: `brew install pipx && pipx ensurepath`
  - Debian/Ubuntu: `sudo apt install pipx && pipx ensurepath`
  - Any Python: `python3 -m pip install --user pipx && python3 -m pipx ensurepath`

> If you want to skip the GitHub check entirely and rely only on manual
> private-repo selection, run `ALLOW_NO_GH=1 make docker-collect`.

## Quick start — Andela pilot

### Docker (recommended)

```bash
git clone --depth 1 https://github.com/SkillBench-AI/session-collector.git
cd session-collector
make docker-collect ALLOWED_ORGS="andela-technology woven-teams woven-reviews"
```

Prefer a token to installing `gh`? Prefix the same command with `GH_TOKEN=…`
(leading space keeps it out of shell history):

```bash
 GH_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx make docker-collect ALLOWED_ORGS="andela-technology woven-teams woven-reviews"
```

See [docs/gh-token.md](docs/gh-token.md) for token scopes and safe handling.

### One-liner install (macOS/Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/andela-collect.sh | bash
```

Re-runs later: `skillbench collect --allowed-orgs andela-technology woven-teams woven-reviews`.

## Quick start — general use

### Docker (recommended)

```bash
git clone --depth 1 https://github.com/SkillBench-AI/session-collector.git
cd session-collector
make docker-collect      # public + OSS repos only (safe default)
make docker-collect-all  # also include private/unlicensed workspaces (opt-in)
```

Without installing `gh`, prefix with a Personal Access Token (leading space
keeps it out of shell history; see [docs/gh-token.md](docs/gh-token.md)):

```bash
 GH_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx make docker-collect
```

### One-liner install (macOS/Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/collect.sh | bash
```

Re-runs later: `skillbench collect`.

### Manual pipx install

```bash
git clone --depth 1 https://github.com/SkillBench-AI/session-collector.git
cd session-collector
pipx install .
skillbench collect
```

## Output

Sanitized weekly exports land in `dist/` on your host
(e.g. `dist/skillbench_export_sanitized_2026_W16.json`). Drag-and-drop those
files — and only those — into the **Upload Data** page of your SkillBench
dashboard. The sanitizer redacts API keys, emails, private IPs, home paths,
and other common secrets before writing.

The dashboard URL is shared separately by your SkillBench point of contact.
If you don't have it yet or you run into issues, reach out to the SkillBench
team.

## More

- **Using `GH_TOKEN` without installing `gh`:** [docs/gh-token.md](docs/gh-token.md)
- **Privacy & data policy:** [docs/privacy.md](docs/privacy.md)
- **Flags, Makefile knobs, step-by-step pipeline, CASS mode:** [docs/details.md](docs/details.md)
- **Design spec:** [SPEC.md](SPEC.md)
- **Recent changes:** [commit history](https://github.com/SkillBench-AI/session-collector/commits/main).
