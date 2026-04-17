# Using `GH_TOKEN` instead of installing the `gh` CLI

You don't have to install GitHub CLI. A GitHub Personal Access Token is
enough, which is handy on ephemeral machines or if you just don't want
another brew/apt package.

## 1. Create a token

GitHub → Settings → Developer settings → Personal access tokens.

- **Fine-grained (recommended).** Resource owner = your user or the org you
  want to classify. Repository access = "All repositories" (or the specific
  repos you care about). Repository permissions:
  - `Metadata: Read-only` — required
  - `Contents: Read-only` — required for license detection
  - Everything else: leave at "No access"
- **Classic.** Select the `repo` scope (needed so `gh repo view` works on
  private repos).

Expiration: 30–90 days is a good default; the tool only needs it during
collection runs.

## 2. Use it for a single run

```bash
 GH_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx make docker-collect ALLOWED_ORGS="your-org"
```

- Note the leading **space**: if your shell has `HISTCONTROL=ignorespace`
  (zsh and bash default on many distros), the command won't be saved to
  shell history, so your token doesn't end up in `~/.zsh_history`.
- The Makefile forwards `GH_TOKEN`/`GITHUB_TOKEN` into the container
  automatically, so it works for both Docker and direct `skillbench collect`.

## 3. Reuse across runs (current shell only)

```bash
export GH_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
make docker-collect ALLOWED_ORGS="your-org"
# …
unset GH_TOKEN      # clear when done
```

## 4. Revoke when you're done

GitHub → Settings → Developer settings → Personal access tokens → Delete.
Treat any token that touched a chat log, screen share, or public paste site
as compromised and rotate immediately.

## Why not put the token in a dotfile?

You can (`~/.zshrc` / `~/.envrc` / `direnv`), but any persisted copy is a
credential you have to manage. For a short-lived pilot, one-off runs with
`HISTCONTROL=ignorespace` are simpler and safer.
