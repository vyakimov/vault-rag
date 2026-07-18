"""Tests for vault_spider.settings (the optional config.yaml)."""

from __future__ import annotations

import json

import pytest
from conftest import write_config

from vault_spider import cli, settings


@pytest.fixture(autouse=True)
def _isolated(isolated_config):
    """Every test in this module runs against the shared isolated config."""
    yield


class TestDefaults:
    def test_no_config_file_uses_defaults(self):
        assert settings.vault_root() is None
        assert settings.skip_dirs() == {".trash", ".obsidian", "Templates"}
        assert settings.ignore_tags() == ["ignore", "secret"]
        assert settings.distilled_dir() == "Distilled"
        assert settings.chroma_path() == str(settings.config_path().parent / "chroma_db")
        assert settings.timestamp_policy() == "offset_local"

    def test_defaults_carry_no_personal_paths(self):
        """The shipped defaults must not assume anyone's particular vault."""
        assert settings.DEFAULTS["vault"]["root"] is None
        assert "999 Templates" not in settings.DEFAULTS["vault"]["skip_dirs"]


class TestOverrides:
    def test_partial_config_overlays_defaults(self, isolated_config):
        write_config(isolated_config, "vault:\n  distilled_dir: Derived\n")
        assert settings.distilled_dir() == "Derived"
        # Untouched keys keep their defaults.
        assert settings.ignore_tags() == ["ignore", "secret"]
        assert settings.chroma_path() == str(isolated_config / "chroma_db")

    def test_relative_filesystem_paths_are_config_local(
        self, isolated_config, monkeypatch
    ):
        write_config(
            isolated_config,
            "vault:\n"
            "  root: vault\n"
            "index:\n"
            "  chroma_path: chroma_db\n"
            "obsidian:\n"
            "  binary: bin/obsidian\n",
        )
        launch_dir = isolated_config / "elsewhere"
        launch_dir.mkdir()
        monkeypatch.chdir(launch_dir)

        assert settings.vault_root() == str(isolated_config / "vault")
        assert settings.chroma_path() == str(isolated_config / "chroma_db")
        assert settings.obsidian_binary() == str(isolated_config / "bin" / "obsidian")

    def test_absolute_filesystem_paths_are_unchanged(self, isolated_config):
        vault = isolated_config / "absolute-vault"
        chroma = isolated_config / "absolute-chroma"
        binary = isolated_config / "absolute-obsidian"
        write_config(
            isolated_config,
            "vault:\n"
            f"  root: {vault}\n"
            "index:\n"
            f"  chroma_path: {chroma}\n"
            "obsidian:\n"
            f"  binary: {binary}\n",
        )

        assert settings.vault_root() == str(vault)
        assert settings.chroma_path() == str(chroma)
        assert settings.obsidian_binary() == str(binary)

    def test_skip_dirs_and_root(self, isolated_config):
        write_config(
            isolated_config,
            "vault:\n  root: ~/somewhere/Vault\n  skip_dirs: [.trash, '999 Templates']\n",
        )
        assert settings.skip_dirs() == {".trash", "999 Templates"}
        assert settings.vault_root().endswith("/somewhere/Vault")
        assert "~" not in settings.vault_root()  # expanded

    def test_ignore_tags_normalized(self, isolated_config):
        write_config(isolated_config, "vault:\n  ignore_tags: ['#Private', 'SECRET']\n")
        assert settings.ignore_tags() == ["private", "secret"]

    def test_utc_z_policy(self, isolated_config):
        write_config(isolated_config, "timestamps:\n  policy: utc_z\n")
        assert settings.timestamp_policy() == "utc_z"

    def test_obsidian_local_policy(self, isolated_config):
        write_config(isolated_config, "timestamps:\n  policy: obsidian_local\n")
        assert settings.timestamp_policy() == "obsidian_local"


class TestErrors:
    def test_unknown_section_rejected(self, isolated_config):
        write_config(isolated_config, "nonsense:\n  a: 1\n")
        with pytest.raises(settings.ConfigError, match="unknown section"):
            settings.vault_root()

    def test_unknown_key_rejected(self, isolated_config):
        write_config(isolated_config, "vault:\n  rooot: /typo\n")
        with pytest.raises(settings.ConfigError, match="unknown key"):
            settings.vault_root()

    def test_bad_policy_rejected(self, isolated_config):
        write_config(isolated_config, "timestamps:\n  policy: whenever\n")
        with pytest.raises(settings.ConfigError, match="offset_local"):
            settings.timestamp_policy()

    def test_malformed_yaml_still_prints_one_json_envelope(self, isolated_config, capsys):
        """A broken config must not break the JSON-only stdout contract."""
        write_config(isolated_config, "vault: [unclosed\n")

        code = cli.main(["lint", "--root", str(isolated_config)])
        envelope = json.loads(capsys.readouterr().out)

        assert code == 1
        assert envelope["ok"] is False
        assert envelope["error"]["type"] == "invalid_arguments"


class TestLoaderHonoursConfig:
    def test_configured_skip_dir_is_not_indexed(self, isolated_config):
        from vault_spider.corpus.loader import load_notes

        write_config(isolated_config, "vault:\n  skip_dirs: ['999 Templates']\n")
        vault = isolated_config / "vault"
        (vault / "999 Templates").mkdir(parents=True)
        (vault / "999 Templates" / "Daily.md").write_text("template", encoding="utf-8")
        (vault / "keep.md").write_text("real note", encoding="utf-8")

        assert [n.path for n in load_notes(str(vault))] == ["keep.md"]

    def test_configured_ignore_tag_is_honoured(self, isolated_config):
        from vault_spider.corpus.loader import load_notes

        write_config(isolated_config, "vault:\n  ignore_tags: [private]\n")
        vault = isolated_config / "vault"
        vault.mkdir()
        (vault / "hidden.md").write_text("this is #private", encoding="utf-8")
        (vault / "keep.md").write_text("this is #secret", encoding="utf-8")  # no longer ignored

        assert [n.path for n in load_notes(str(vault))] == ["keep.md"]
