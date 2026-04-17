import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import skillbench


def test_extract_github_org_from_remote_handles_github_urls():
    assert (
        skillbench.extract_github_org_from_remote(
            "git@github.com:Andela-Technology/platform.git"
        )
        == "andela-technology"
    )
    assert (
        skillbench.extract_github_org_from_remote(
            "https://github.com/woven-teams/example.git"
        )
        == "woven-teams"
    )
    assert (
        skillbench.extract_github_org_from_remote(
            "git@github.com-andela:andela-technology/andela.git"
        )
        == "andela-technology"
    )
    assert (
        skillbench.extract_github_org_from_remote(
            "git@github-andela:Andela-Technology/platform.git"
        )
        == "andela-technology"
    )
    assert (
        skillbench.extract_github_org_from_remote(
            "ssh://git@github-andela/Andela-Technology/platform.git"
        )
        == "andela-technology"
    )
    assert (
        skillbench.extract_github_org_from_remote(
            "https://gitlab.com/andela/platform.git"
        )
        is None
    )
    assert (
        skillbench.extract_github_org_from_remote(
            "git@andela-github:andela-technology/andela.git"
        )
        == "andela-technology"
    )


def test_get_repo_scope_decision_matches_allowed_orgs():
    allowed = ["andela-technology", "woven-teams"]

    approved = skillbench.get_repo_scope_decision(
        "git@github.com:Andela-Technology/platform.git",
        allowed,
    )
    assert approved == {
        "allowed": True,
        "scope": "approved",
        "classification": "github_org_match",
        "remote_org": "andela-technology",
    }

    external = skillbench.get_repo_scope_decision(
        "git@github.com:someone/private-repo.git",
        allowed,
    )
    assert external == {
        "allowed": False,
        "scope": "external",
        "classification": "github_org_mismatch",
        "remote_org": "someone",
    }

    unknown = skillbench.get_repo_scope_decision(
        "https://gitlab.com/andela/platform.git",
        allowed,
    )
    assert unknown == {
        "allowed": False,
        "scope": "unknown",
        "classification": "no_github_remote",
        "remote_org": None,
    }


def test_classify_workspace_entry_blocks_external_private_repo():
    info = {
        "agents": ["claude"],
        "total_conversations": 3,
        "total_user_messages": 9,
    }

    with patch.object(
        skillbench,
        "git_remote_url",
        return_value="git@github.com:someone/private-repo.git",
    ), patch.object(
        skillbench,
        "classify_github_repo",
        return_value={"is_public": False, "license_key": None, "license_name": None},
    ):
        entry = skillbench.classify_workspace_entry(
            "/tmp/repo",
            info,
            ["andela-technology"],
        )

    assert entry["remote_org"] == "someone"
    assert entry["selection_allowed"] is False
    assert entry["auto_include"] is False
    assert "outside allowed orgs (someone)" in entry["reason"]
    assert "private repo" in entry["reason"]


def test_classify_workspace_entry_keeps_allowed_private_repo_selectable():
    info = {
        "agents": ["claude"],
        "total_conversations": 2,
        "total_user_messages": 4,
    }

    with patch.object(
        skillbench,
        "git_remote_url",
        return_value="git@github.com:woven-teams/private-repo.git",
    ), patch.object(
        skillbench,
        "classify_github_repo",
        return_value={"is_public": False, "license_key": None, "license_name": None},
    ):
        entry = skillbench.classify_workspace_entry(
            "/tmp/repo",
            info,
            ["andela-technology", "woven-teams"],
        )

    assert entry["remote_org"] == "woven-teams"
    assert entry["selection_allowed"] is True
    assert entry["auto_include"] is False
    assert entry["reason"] == "private repo, no LICENSE file"


def test_collect_default_allowed_orgs_are_pilot_defaults():
    with patch.object(skillbench, "cmd_collect") as cmd_collect:
        with patch.object(sys, "argv", ["skillbench", "collect"]):
            skillbench.main()

    args = cmd_collect.call_args.args[0]
    assert args.allowed_orgs == skillbench.PILOT_ALLOWED_GITHUB_ORGS


def test_collect_prompt_displays_git_remote_org_for_selectable_workspaces(capsys, tmp_path):
    class FakeScanner:
        def __init__(self):
            self.conversations = [object()]

        def scan_all(self, verbose=True):
            return None

        def resolve_gemini_hashes(self):
            return 0

        def get_workspace_summary(self):
            return [
                {
                    "workspace": str(tmp_path / "allowed-private"),
                    "agents": ["claude"],
                    "total_conversations": 2,
                    "total_user_messages": 5,
                },
                {
                    "workspace": str(tmp_path / "blocked-private"),
                    "agents": ["codex"],
                    "total_conversations": 1,
                    "total_user_messages": 2,
                },
            ]

    class FakeSanitizer:
        pass

    for folder in ("allowed-private", "blocked-private"):
        (tmp_path / folder).mkdir()

    def fake_git_remote(path):
        if path.endswith("allowed-private"):
            return "git@github.com:andela-technology/private.git"
        return "git@github.com:someone/other.git"

    def fake_gh(remote):
        return {"is_public": False, "license_key": None, "license_name": None}

    fake_session_parser = SimpleNamespace(SessionScanner=FakeScanner)
    with patch.dict(
        sys.modules,
        {
            "session_parser": fake_session_parser,
            "sanitizer": SimpleNamespace(Sanitizer=FakeSanitizer),
        },
    ), patch.object(skillbench, "git_remote_url", side_effect=fake_git_remote), patch.object(
        skillbench,
        "is_skippable",
        return_value=False,
    ), patch.object(
        skillbench,
        "classify_github_repo",
        side_effect=fake_gh,
    ), patch(
        "builtins.input",
        side_effect=["n"],
    ):
        args = Namespace(output=None, yes=False, include_excluded=False, allowed_orgs=[
            "andela-technology",
            "woven-teams",
            "woven-reviews",
        ])
        with pytest.raises(SystemExit) as exc_info:
            skillbench.cmd_collect(args)

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    # selectable row shows the detected GitHub org inline
    assert "org: andela-technology" in output
    # blocked rows still surface the blocked workspace + reason
    assert "blocked-private" in output
    assert "outside allowed orgs (someone)" in output


def test_git_remote_url_falls_back_to_non_origin_github_remote(tmp_path):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        """
[core]
    repositoryformatversion = 0
[remote "upstream"]
    url = git@github-andela:Andela-Technology/platform.git
""".strip()
    )

    with patch.object(skillbench.subprocess, "run") as run:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        assert (
            skillbench.git_remote_url(str(repo))
            == "git@github-andela:Andela-Technology/platform.git"
        )


def test_git_remote_url_prefers_github_remote_over_non_github_origin(tmp_path):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        """
[core]
    repositoryformatversion = 0
[remote "origin"]
    url = ssh://git@gitlab.internal/team/platform.git
[remote "upstream"]
    url = git@github-andela:Andela-Technology/platform.git
""".strip()
    )

    with patch.object(skillbench.subprocess, "run") as run:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        assert (
            skillbench.git_remote_url(str(repo))
            == "git@github-andela:Andela-Technology/platform.git"
        )


def test_expand_git_url_insteadof_andela_prefix():
    rules = skillbench._parse_url_insteadof_rules(
        "url.git@andela-github:andela-technology.insteadof=andela:\n",
    )
    expanded = skillbench._expand_git_url_insteadof("andela:/andela.git", rules)
    assert expanded == "git@andela-github:andela-technology/andela.git"


def test_extract_github_org_from_remote_resolves_ssh_alias_via_ssh_g():
    def fake_run(cmd, **kwargs):
        r = SimpleNamespace(returncode=0, stdout="")
        if cmd[:2] == ["ssh", "-G"]:
            r.stdout = "hostname github.com\nuser git\n"
        else:
            r.returncode = 1
        return r

    with patch.object(skillbench.subprocess, "run", side_effect=fake_run):
        assert (
            skillbench.extract_github_org_from_remote(
                "git@corp-gh:andela-technology/platform.git"
            )
            == "andela-technology"
        )


def test_git_remote_url_prefers_git_remote_get_url_resolution():
    def fake_run(cmd, **kwargs):
        r = SimpleNamespace(returncode=0, stdout="")
        if cmd[:4] == ["git", "-C", "/tmp/repo", "config"] and "-l" in cmd:
            r.stdout = ""
        elif cmd == ["git", "-C", "/tmp/repo", "remote"]:
            r.stdout = "origin\nupstream\n"
        elif cmd == ["git", "-C", "/tmp/repo", "remote", "get-url", "origin"]:
            r.stdout = "ssh://git@gitlab.internal/team/platform.git\n"
        elif cmd == ["git", "-C", "/tmp/repo", "remote", "get-url", "upstream"]:
            r.stdout = "git@github-andela:Andela-Technology/platform.git\n"
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with patch.object(skillbench, "_nearest_git_repo_root", return_value="/tmp/repo"), patch.object(
        skillbench.subprocess, "run", side_effect=fake_run
    ):
        assert (
            skillbench.git_remote_url("/tmp/repo")
            == "git@github-andela:Andela-Technology/platform.git"
        )


def test_git_remote_url_expands_global_insteadof(tmp_path):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        """
[core]
    repositoryformatversion = 0
[remote "origin"]
    url = andela:/andela.git
""".strip()
    )

    def fake_run(cmd, **kwargs):
        r = SimpleNamespace(returncode=0, stdout="")
        if cmd[:2] == ["git", "-C"] and "config" in cmd and "-l" in cmd:
            r.stdout = "url.git@andela-github:andela-technology.insteadof=andela:\n"
        elif "--get-regexp" in cmd:
            r.stdout = "remote.origin.url andela:/andela.git\n"
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with patch.object(skillbench.subprocess, "run", side_effect=fake_run):
        assert (
            skillbench.git_remote_url(str(repo))
            == "git@andela-github:andela-technology/andela.git"
        )


def test_git_remote_url_subfolder_uses_parent_repo(tmp_path):
    root = tmp_path / "mono"
    sub = root / "backend"
    sub.mkdir(parents=True)
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        """
[core]
    repositoryformatversion = 0
[remote "origin"]
    url = andela:/andela.git
""".strip()
    )

    def fake_run(cmd, **kwargs):
        r = SimpleNamespace(returncode=0, stdout="")
        if cmd[:2] == ["git", "-C"] and "config" in cmd and "-l" in cmd:
            r.stdout = "url.git@andela-github:andela-technology.insteadof=andela:\n"
        elif "--get-regexp" in cmd:
            assert cmd[2] == str(root)
            r.stdout = "remote.origin.url andela:/andela.git\n"
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with patch.object(skillbench.subprocess, "run", side_effect=fake_run):
        assert (
            skillbench.git_remote_url(str(sub))
            == "git@andela-github:andela-technology/andela.git"
        )
