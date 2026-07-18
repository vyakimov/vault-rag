"""Tests for scripts/setup_obsidian.py (no network and no real Obsidian)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import setup_obsidian  # pyright: ignore[reportMissingImports]  # noqa: E402


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_vault(tmp_path: Path, installed: bool = False) -> Path:
    root = tmp_path / "Vault"
    (root / ".obsidian").mkdir(parents=True)
    if installed:
        plugin = root / ".obsidian" / "plugins" / setup_obsidian.PLUGIN_ID
        plugin.mkdir(parents=True)
        (plugin / "manifest.json").write_text("{}", encoding="utf-8")
        (plugin / "main.js").write_text("// plugin", encoding="utf-8")
    return root


def envelope(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_dry_run_reports_plan_without_writing(tmp_path, capsys):
    root = make_vault(tmp_path)

    code = setup_obsidian.main(["--root", str(root)])
    result = envelope(capsys)

    assert code == 0 and result["ok"]
    assert result["result"]["dry_run"] is True
    assert result["result"]["plan"]["plugin"]["installed"] is False
    assert any("install community plugin" in action for action in result["result"]["plan"]["actions"])
    assert list((root / ".obsidian").iterdir()) == []


def test_configure_only_merges_and_preserves_unrelated_settings(tmp_path, capsys):
    root = make_vault(tmp_path, installed=True)
    obsidian = root / ".obsidian"
    plugin_data = obsidian / "plugins" / setup_obsidian.PLUGIN_ID / "data.json"
    write_json(
        plugin_data,
        {
            "dateFormat": "old",
            "minMinutesBetweenSaves": 9,
            "ignoreGlobalFolder": ["Templates"],
            "fileHashMap": {"Note.md": "hash"},
            "custom": "keep",
        },
    )
    write_json(obsidian / "types.json", {"types": {"aliases": "multitext"}})
    write_json(
        obsidian / "app.json",
        {"alwaysUpdateLinks": False, "attachmentFolderPath": "attachments"},
    )
    write_json(obsidian / "core-plugins.json", {"properties": False, "daily-notes": False})
    write_json(obsidian / "community-plugins.json", ["other-plugin"])

    code = setup_obsidian.main(
        ["--root", str(root), "--apply", "--configure-only"]
    )
    result = envelope(capsys)

    assert code == 0 and result["ok"]
    data = json.loads(plugin_data.read_text(encoding="utf-8"))
    assert data["dateFormat"] == "yyyy-MM-dd'T'HH:mm:ss"
    assert data["enableCreateTime"] is False
    assert data["headerUpdated"] == "updated"
    assert data["headerCreated"] == "created"
    assert data["minMinutesBetweenSaves"] == 9
    assert data["ignoreGlobalFolder"] == ["Templates"]
    assert data["fileHashMap"] == {"Note.md": "hash"}
    assert data["custom"] == "keep"

    types = json.loads((obsidian / "types.json").read_text(encoding="utf-8"))["types"]
    assert types == {"aliases": "multitext", "created": "datetime", "updated": "datetime"}
    app = json.loads((obsidian / "app.json").read_text(encoding="utf-8"))
    assert app == {"alwaysUpdateLinks": True, "attachmentFolderPath": "attachments"}
    core = json.loads((obsidian / "core-plugins.json").read_text(encoding="utf-8"))
    assert core == {"properties": True, "daily-notes": False}
    enabled = json.loads((obsidian / "community-plugins.json").read_text(encoding="utf-8"))
    assert enabled == ["other-plugin", setup_obsidian.PLUGIN_ID]
    assert Path(result["result"]["backup_dir"]).is_dir()

    second = setup_obsidian.build_plan(root)
    assert second["actions"] == []


def test_minute_override_is_explicit(tmp_path, capsys):
    root = make_vault(tmp_path, installed=True)
    plugin_data = root / ".obsidian" / "plugins" / setup_obsidian.PLUGIN_ID / "data.json"
    write_json(plugin_data, {"minMinutesBetweenSaves": 9})
    write_json(root / ".obsidian" / "community-plugins.json", [setup_obsidian.PLUGIN_ID])

    code = setup_obsidian.main(
        [
            "--root",
            str(root),
            "--apply",
            "--configure-only",
            "--min-update-minutes",
            "2",
        ]
    )
    envelope(capsys)

    assert code == 0
    data = json.loads(plugin_data.read_text(encoding="utf-8"))
    assert data["minMinutesBetweenSaves"] == 2


def test_normal_apply_uses_safe_cli_sequence(tmp_path, capsys, monkeypatch):
    root = make_vault(tmp_path, installed=True)
    obsidian = root / ".obsidian"
    write_json(obsidian / "community-plugins.json", [setup_obsidian.PLUGIN_ID])
    commands: list[list[str]] = []
    monkeypatch.setattr(setup_obsidian, "_resolve_binary", lambda explicit: "/fake/obsidian")
    monkeypatch.setattr(
        setup_obsidian,
        "_run_cli",
        lambda binary, vault, arguments: commands.append(arguments) or "ok",
    )

    code = setup_obsidian.main(
        ["--root", str(root), "--vault", "Test Vault", "--apply"]
    )
    result = envelope(capsys)

    assert code == 0 and result["ok"]
    assert commands == [
        ["plugin:disable", f"id={setup_obsidian.PLUGIN_ID}", "filter=community"],
        ["plugin:enable", f"id={setup_obsidian.PLUGIN_ID}", "filter=community"],
        ["reload"],
    ]


def test_compliant_apply_is_a_noop(tmp_path, capsys, monkeypatch):
    root = make_vault(tmp_path, installed=True)
    setup_obsidian._write_configuration(root, None, community=True)
    monkeypatch.setattr(
        setup_obsidian,
        "_run_cli",
        lambda *args: (_ for _ in ()).throw(AssertionError("CLI should not run")),
    )

    code = setup_obsidian.main(
        ["--root", str(root), "--vault", "Test Vault", "--apply"]
    )
    result = envelope(capsys)

    assert code == 0
    assert result["result"]["applied"] == []
    assert result["result"]["backup_dir"] is None


def test_failure_reenables_previously_enabled_plugin(tmp_path, capsys, monkeypatch):
    root = make_vault(tmp_path, installed=True)
    write_json(root / ".obsidian" / "community-plugins.json", [setup_obsidian.PLUGIN_ID])
    commands: list[list[str]] = []
    monkeypatch.setattr(setup_obsidian, "_resolve_binary", lambda explicit: "/fake/obsidian")
    monkeypatch.setattr(
        setup_obsidian,
        "_run_cli",
        lambda binary, vault, arguments: commands.append(arguments) or "ok",
    )
    monkeypatch.setattr(
        setup_obsidian,
        "_write_configuration",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    code = setup_obsidian.main(
        ["--root", str(root), "--vault", "Test Vault", "--apply"]
    )
    result = envelope(capsys)

    assert code == 1
    assert "disk full" in result["error"]["message"]
    assert commands == [
        ["plugin:disable", f"id={setup_obsidian.PLUGIN_ID}", "filter=community"],
        ["plugin:enable", f"id={setup_obsidian.PLUGIN_ID}", "filter=community"],
    ]


def test_malformed_existing_json_fails_without_overwriting(tmp_path, capsys):
    root = make_vault(tmp_path, installed=True)
    path = root / ".obsidian" / "types.json"
    path.write_text("{broken", encoding="utf-8")

    code = setup_obsidian.main(
        ["--root", str(root), "--apply", "--configure-only"]
    )
    result = envelope(capsys)

    assert code == 1 and result["ok"] is False
    assert result["error"]["type"] == "setup_error"
    assert path.read_text(encoding="utf-8") == "{broken"


def test_configure_only_requires_installed_plugin(tmp_path, capsys):
    root = make_vault(tmp_path)

    code = setup_obsidian.main(
        ["--root", str(root), "--apply", "--configure-only"]
    )
    result = envelope(capsys)

    assert code == 1
    assert "is not installed" in result["error"]["message"]


def test_argument_errors_are_json(capsys):
    code = setup_obsidian.main(["--min-update-minutes", "not-a-number"])
    result = envelope(capsys)

    assert code == 1
    assert result["error"]["type"] == "setup_error"
