"""Tests for `skillbench config`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skillbench  # noqa: E402
from skillbench.config import Config, parse_value  # noqa: E402


def test_config_set_get_unset_round_trip(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("SKILLBENCH_CONFIG", str(cfg_path))

    cfg = Config.load()
    cfg.set("codex.allowed_orgs", ["acme", "matthew"])
    cfg.set("codex.interval", 60)
    cfg.save()

    reloaded = Config.load()
    assert reloaded.get("codex.allowed_orgs") == ["acme", "matthew"]
    assert reloaded.get("codex.interval") == 60

    assert reloaded.unset("codex.interval")
    reloaded.save()

    assert Config.load().get("codex.interval") is None


def test_parse_value_validates_keys_and_types():
    assert parse_value("codex.allowed_orgs", ["a,b", "c"]) == ["a", "b", "c"]
    assert parse_value("codex.interval", ["45"]) == 45

    try:
        parse_value("not.a.real.key", ["x"])
    except ValueError as exc:
        assert "Unknown config key" in str(exc)
    else:
        raise AssertionError("should reject unknown keys")

    try:
        parse_value("codex.interval", ["soon"])
    except ValueError as exc:
        assert "expects an integer" in str(exc)
    else:
        raise AssertionError("should reject non-integer interval")


def test_main_routes_config_show_set_get(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("SKILLBENCH_CONFIG", str(cfg_path))

    with patch.object(
        sys, "argv",
        ["skillbench", "config", "set", "codex.allowed_orgs", "acme,matthew", "research"],
    ):
        skillbench.main()

    payload = json.loads(cfg_path.read_text())
    assert payload["codex"]["allowed_orgs"] == ["acme", "matthew", "research"]

    capsys.readouterr()
    with patch.object(sys, "argv", ["skillbench", "config", "get", "codex.allowed_orgs"]):
        skillbench.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["acme", "matthew", "research"]

    with patch.object(sys, "argv", ["skillbench", "config", "show"]):
        skillbench.main()
    out = capsys.readouterr().out
    assert "codex.allowed_orgs" in out
