# session-collector

Collect and analyze your AI coding sessions locally. No data leaves your machine until you share the sanitized export.

Supports local session collection from Claude Code, Gemini CLI, and Codex.

## Prerequisites

Install once, on the host:

- **GitHub authentication** — either install the GitHub CLI **or** use a Personal Access Token:
  - `gh` CLI: `brew install gh && gh auth login` (macOS) / `sudo apt install gh && gh auth login` / [cli.github.com](https://cli.github.com/)
  - Or set `GH_TOKEN=<personal access token>` before running — see [docs/gh-token.md](docs/gh-token.md) for scopes and safe handling.
- **git** — needed to read remotes from your project folders.
- **Python 3.9+** and **[pipx](https://pipx.pypa.io/)**:
  - macOS: `brew install pipx && pipx ensurepath`
  - Debian / Ubuntu: `sudo apt install pipx && pipx ensurepath`
  - Any Python: `python3 -m pip install --user pipx && python3 -m pipx ensurepath`
- *(Docker workflow only)* **Docker** — macOS / Linux / Windows via Docker Desktop.

> Want to skip the GitHub check entirely and rely only on manual private-repo
> selection? Run `ALLOW_NO_GH=1 make docker-collect`.

## Getting started

Pick whichever fits your setup — they produce the same sanitized export.

### Install from PyPI (recommended)

```bash
pipx install skillbench-session-collector
skillbench collect                                             # all orgs
skillbench collect --allowed-orgs your-company your-username   # restrict scope
```

To upgrade later:

```bash
pipx upgrade skillbench-session-collector
```

### One-liner install (macOS / Linux)

Installs the published package via `pipx`:

```bash
curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/install.sh | bash
```

The installer does not clone this repository. It checks Python 3.9+ and `pipx`,
then runs `pipx install --force skillbench-session-collector`.

### One-liner collect (macOS / Linux)

Installs the published package via `pipx` and runs `skillbench collect` in one
step:

```bash
curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/collect.sh | bash
```

Re-runs later (no installer needed):

```bash
skillbench collect                                             # all orgs
skillbench collect --allowed-orgs your-company your-username   # restrict scope
```

### Docker

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

Prefer a token to installing `gh`? Prefix the same command with `GH_TOKEN=...`
(leading space keeps it out of shell history). See [docs/gh-token.md](docs/gh-token.md)
for token scopes and safe handling.

## Codex workflow (recommended)

For Codex users we recommend the high-level `skillbench codex` surface plus
`skillbench doctor` for setup checks. These wrap the lower-level `daemon-*`
commands in friendlier names and add a one-command happy path.

### One-command installer

```bash
curl -fsSL https://raw.githubusercontent.com/SkillBench-AI/session-collector/main/install.sh | bash
```

The installer checks for Python 3.9+ and `pipx`, then installs
`skillbench-session-collector` so `skillbench` ends up on your PATH.

### Health check

```bash
skillbench doctor
```

Verifies Python version, that the CLI is on PATH, that Codex session
directories exist, that sessions were detected, and that the local SQLite
database and `dist/` are writable. Each failure includes a remediation hint.

### One-command happy path

```bash
skillbench codex collect --allowed-orgs your-company your-github-user
```

Runs scan + a single ingest pass + a sanitized export in one step:

```text
SkillBench Codex collection complete.

  Scanned sessions:  14
  Daemon updates:    inserted=8 updated=0 unchanged=0 removed=0 failed=0
  Exported sessions: 8

  Sanitized export written to:
    dist/skillbench_daemon_export_sanitized.json
```

### Friendly aliases

| User-facing command            | Wraps              |
|-------------------------------|--------------------|
| `skillbench codex scan`       | `daemon-scan`      |
| `skillbench codex status`     | `daemon-status`    |
| `skillbench codex watch`      | `daemon-run`       |
| `skillbench codex export`     | `daemon-export`    |
| `skillbench codex collect`    | scan + run-once + sanitized export |
| `skillbench codex locate-sessions` | diagnose `scanned=0` |
| `skillbench codex plugin-install`  | clone marketplace + register with Codex |

### Codex plugin install

```bash
skillbench codex plugin-install
```

Clones (or refreshes) the SkillMeter Codex marketplace and runs
`codex plugin marketplace add` for you. After it finishes:

```text
1. Open Codex
2. Run /plugins
3. Install SkillMeter
4. Start a fresh Codex thread
5. Try @skillmeter check my collector status
```

Pass `--dry-run` to see the commands without executing them.

### Saved config

Avoid passing `--allowed-orgs` every time:

```bash
skillbench config set codex.allowed_orgs your-company your-github-user
skillbench codex collect          # uses saved orgs automatically
```

`skillbench config show` prints the current values. The file lives at
`~/.skillbench/config.json` (override with `SKILLBENCH_CONFIG=...`).

### Troubleshooting `scanned=0`

```bash
skillbench codex locate-sessions
```

Probes every well-known Codex session root and reports per-path counts. If
your sessions are stored elsewhere, point the tooling at them with
`--base-dir /path/to/codex/sessions` or save it via
`skillbench config set codex.base_dir /path/to/codex/sessions`.

### Raw exports

Sanitized export is the default. To produce a raw, un-sanitized export you
must opt in explicitly:

```bash
skillbench codex export --raw --i-understand-this-may-include-sensitive-data
SKILLBENCH_ALLOW_RAW_EXPORT=1 skillbench daemon-export --raw
```

Without the flag (or env var) the CLI refuses to write a raw export.

### Lower-level daemon commands

The original `daemon-*` commands are still available for power users:

```bash
skillbench daemon-scan
skillbench daemon-run --interval 30
skillbench daemon-status
skillbench daemon-export --allowed-orgs your-company your-username
```

Equivalent Make targets exist (`make daemon-scan`, `make doctor`,
`make codex-collect ALLOWED_ORGS="..."`, `make codex-plugin-install`).

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
- **Publishing releases:** [docs/releasing.md](docs/releasing.md)
- **Recent changes:** [commit history](https://github.com/SkillBench-AI/session-collector/commits/main).
