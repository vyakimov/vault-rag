"""Tests for the launchd plist renderer; no real service is loaded."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import setup_launchd  # pyright: ignore[reportMissingImports]  # noqa: E402


def test_build_plist_uses_hourly_sync_only_defaults(tmp_path):
    repo = tmp_path / "vault-spider"
    home = tmp_path / "home"
    uv = home / ".local" / "bin" / "uv"

    plist = setup_launchd.build_plist(repo, uv, home, 60, False)

    assert plist["Label"] == "ai.vault-spider.sync"
    assert plist["ProgramArguments"] == [
        str(uv),
        "run",
        "--project",
        str(repo),
        "python",
        str(repo / "scripts" / "periodic_maintenance.py"),
    ]
    assert plist["WorkingDirectory"] == str(repo)
    assert plist["StartInterval"] == 3600
    assert plist["RunAtLoad"] is True
    assert plist["EnvironmentVariables"]["VAULT_SPIDER_RUN_LINT"] == "0"
    assert str(uv.parent) in plist["EnvironmentVariables"]["PATH"]
    assert plist["StandardOutPath"] == str(
        home / "Library" / "Logs" / "VaultSpider" / "sync.stdout.log"
    )


def test_build_plan_exposes_lint_but_never_enrichment(tmp_path, monkeypatch):
    repo = tmp_path / "vault-spider"
    home = tmp_path / "home"
    uv = home / ".local" / "bin" / "uv"
    monkeypatch.setattr(setup_launchd, "_loaded", lambda: False)

    plan = setup_launchd.build_plan(repo, uv, home, 30, True, False)

    assert plan["interval_minutes"] == 30
    assert plan["lint_enabled"] is True
    assert plan["enrich_enabled"] is False
    assert plan["plist"]["EnvironmentVariables"]["VAULT_SPIDER_RUN_LINT"] == "1"


def test_too_short_interval_is_json_failure(capsys):
    code = setup_launchd.main(["--interval-minutes", "4"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["type"] == "setup_error"
    assert "at least 5" in payload["error"]["message"]


def test_generated_plist_passes_plutil(tmp_path):
    plist = setup_launchd.build_plist(
        tmp_path / "repo",
        tmp_path / "bin" / "uv",
        tmp_path / "home",
        60,
        False,
    )

    payload = setup_launchd._validated_plist_bytes(plist)

    assert payload.startswith(b"<?xml")


def test_validation_falls_back_to_plistlib_without_plutil(tmp_path, monkeypatch):
    """plutil is macOS-only; Linux (e.g. CI) must still validate via plistlib."""
    plist = setup_launchd.build_plist(
        tmp_path / "repo", tmp_path / "bin" / "uv", tmp_path / "home", 60, False
    )
    monkeypatch.setattr(setup_launchd.shutil, "which", lambda name: None)

    payload = setup_launchd._validated_plist_bytes(plist)

    assert payload.startswith(b"<?xml")


def test_fallback_validation_rejects_invalid_plist(tmp_path, monkeypatch):
    plist = setup_launchd.build_plist(
        tmp_path / "repo", tmp_path / "bin" / "uv", tmp_path / "home", 60, False
    )
    monkeypatch.setattr(setup_launchd.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        setup_launchd.plistlib,
        "loads",
        lambda payload: (_ for _ in ()).throw(
            setup_launchd.plistlib.InvalidFileException("bad")
        ),
    )

    try:
        setup_launchd._validated_plist_bytes(plist)
        assert False, "expected SetupError"
    except setup_launchd.SetupError as exc:
        assert "generated plist is invalid" in str(exc)


def test_apply_starts_once_via_bootstrap_without_kickstart(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    plist = setup_launchd.build_plist(repo, tmp_path / "bin" / "uv", home, 60, False)
    calls: list[list[str]] = []
    monkeypatch.setattr(setup_launchd, "_loaded", lambda: False)
    monkeypatch.setattr(
        setup_launchd,
        "_launchctl",
        lambda arguments, check=True: calls.append(arguments),
    )

    result = setup_launchd.apply_install(plist, home)

    assert result["started"] is True
    assert calls == [
        ["enable", setup_launchd._service_target()],
        [
            "bootstrap",
            f"gui/{setup_launchd.os.getuid()}",
            str(home / "Library" / "LaunchAgents" / "ai.vault-spider.sync.plist"),
        ],
    ]
