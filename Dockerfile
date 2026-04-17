# syntax=docker/dockerfile:1.7
FROM python:3.10-slim

ARG GH_VERSION=""
ENV GH_VERSION=${GH_VERSION}

# Install git (needed for git remote detection inside the container).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*'

# Pre-install Python runtime deps so the interactive picker works in the
# container. Kept minimal — questionary is our only runtime dep for now.
RUN pip install --no-cache-dir "questionary>=2.0"

# Install GitHub CLI (gh) without apt.
# This avoids Homebrew/Xcode and also avoids apt cache space issues in minimal Docker setups.
RUN <<'PY' python3 -
import io
import json
import os
import platform
import tarfile
import urllib.request

gh_version = os.environ.get("GH_VERSION") or ""
if not gh_version:
    with urllib.request.urlopen("https://api.github.com/repos/cli/cli/releases/latest") as r:
        data = json.load(r)
    gh_version = (data.get("tag_name") or "").lstrip("v")
    if not gh_version:
        raise SystemExit("Could not determine latest gh version")

arch = platform.machine().lower()
if arch in ("aarch64", "arm64"):
    asset_arch = "arm64"
elif arch in ("x86_64", "amd64"):
    asset_arch = "amd64"
else:
    raise SystemExit(f"Unsupported arch: {arch}")

url = f"https://github.com/cli/cli/releases/download/v{gh_version}/gh_{gh_version}_linux_{asset_arch}.tar.gz"
print("Downloading", url)
with urllib.request.urlopen(url) as r:
    blob = r.read()

tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
gh_member = next((m for m in tf.getmembers() if m.name.endswith("/bin/gh")), None)
if not gh_member:
    raise SystemExit("Could not find gh binary in tarball")

os.makedirs("/usr/local/bin", exist_ok=True)
out_path = "/usr/local/bin/gh"
with tf.extractfile(gh_member) as src, open(out_path, "wb") as dst:
    dst.write(src.read())
os.chmod(out_path, 0o755)
print("Installed", out_path)
PY

# Default runtime layout.
# We mount the repo at /work and set HOME to a mount-friendly location so
# ~/.claude, ~/.gemini, ~/.codex can be mounted under /home/app/.
# PYTHONPATH=/work/src lets `python -m skillbench` find the package without
# a pip install step (kept out of the image on purpose: code changes in the
# mounted repo are picked up instantly, no rebuild needed).
ENV HOME=/home/app \
    PYTHONPATH=/work/src
WORKDIR /work

# Keep image lightweight; run code directly from bind-mounted repo.
CMD ["python3", "-m", "skillbench", "--help"]

