# session-collector

Collect and analyze your AI coding sessions locally. No data leaves your machine until you share the sanitized export.

## Prerequisites

Install once, on the host:

- **Docker** — for the recommended Docker workflow (macOS / Linux / Windows via Docker Desktop).
- **GitHub authentication** — either install the GitHub CLI **or** use a Personal Access Token:
  - `gh` CLI: `brew install gh && gh auth login` (macOS) / `sudo apt install gh && gh auth login` / [cli.github.com](https://cli.github.com/)
  - Or set `GH_TOKEN=<personal access token>` before running — see [docs/gh-token.md](docs/gh-token.md) for scopes and safe handling.
- **git** — needed to read remotes from your project folders.
- *(Python workflows only)* **Python 3.9+** and **[pipx](https://pipx.pypa.io/)**:
  - macOS: `brew install pipx && pipx ensurepath`
  - Debian / Ubuntu: `sudo apt install pipx && pipx ensurepath`
  - Any Python: `python3 -m pip install --user pipx && python3 -m pipx ensurepath`

> Want to skip the GitHub check entirely and rely only on manual private-repo
> selection? Run `ALLOW_NO_GH=1 make docker-collect`.

## Getting started

Pick whichever fits your setup — they produce the same sanitized export.

### Docker (recommended)

```bash
git clone --depth 1 https://github.com/SkillBench-AI/session-collector.git
cd session-collector
make docker-collect      # public + OSS repos only (safe default)
make docker-collect-all  # also include private / unlicensed workspaces (opt-in)
```

Restrict collection to specific GitHub orgs (e.g. your company + personal account):

```bash
make docker-collect ALLOWED_ORGS="your-company your-github-username"
```

Prefer a token to installing `gh`? Prefix the same command with `GH_TOKEN=…`
(leading space keeps it out of shell history):

```bash
 GH_TOKEN=<YOUR_GITHUB_TOKEN> make docker-collect
```

See [docs/gh-token.md](docs/gh-token.md) for token scopes and safe handling.

### One-liner install (macOS / Linux)

Installs the CLI via `pipx` and runs the collect in one step:

```bash
curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/collect.sh | bash
```

Re-runs later (no installer needed):

```bash
skillbench collect                                             # all orgs
skillbench collect --allowed-orgs your-company your-username   # restrict scope
```

### Manual pipx install

```bash
git clone --depth 1 https://github.com/SkillBench-AI/session-collector.git
cd session-collector
pipx install .
skillbench collect                                             # all orgs
skillbench collect --allowed-orgs your-company your-username   # restrict scope
```

## Output & upload

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
- **Recent changes:** [commit history](https://github.com/SkillBench-AI/session-collector/commits/main).
