# Releasing

The session collector is distributed as the PyPI package
`skillbench-session-collector`. The installed console command remains
`skillbench`.

## One-time PyPI setup

1. Create or claim the `skillbench-session-collector` project on PyPI.
2. Configure PyPI Trusted Publishing for this repository.
3. Use the GitHub Actions environment named `pypi`, matching
   `.github/workflows/publish-pypi.yml`.

## Release flow

1. Update `version` in `pyproject.toml`.
2. Open and merge a PR; CI must pass, including the package build job.
3. Create a GitHub release for the same version tag.
4. The `Publish to PyPI` workflow builds the sdist/wheel and publishes them.

## Smoke test

After publishing:

```bash
pipx install --force skillbench-session-collector
skillbench --help
skillbench doctor
```

For local installer smoke tests before publishing:

```bash
SKILLBENCH_PACKAGE="$PWD" bash install.sh
SKILLBENCH_PACKAGE="$PWD" bash collect.sh --help
```
