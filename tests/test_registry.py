"""Tests for Obsidian registry parsing and read-path root fallback."""

from __future__ import annotations

import json
import os

import pytest
from conftest import write_registry

from vault_spider import cli
from vault_spider.envelope import CliError
from vault_spider.obsidian import registry


class TestRegistryParsing:
    def test_one_open_vault_is_active(self, isolated_obsidian_registry, tmp_path):
        vault = tmp_path / "Vault"
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(vault), "ts": 123, "open": True}},
        )

        assert registry.registered_vaults() == [{"path": str(vault), "open": True}]
        assert registry.active_vault_path() == str(vault)

    def test_zero_open_vaults_is_invalid_arguments(
        self, isolated_obsidian_registry, tmp_path
    ):
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(tmp_path / "Vault"), "open": False}},
        )

        with pytest.raises(CliError) as exc:
            registry.active_vault_path()
        assert exc.value.err_type == "invalid_arguments"

    def test_two_open_vaults_are_ambiguous(self, isolated_obsidian_registry, tmp_path):
        paths = [str(tmp_path / "One"), str(tmp_path / "Two")]
        write_registry(
            isolated_obsidian_registry,
            {
                "one": {"path": paths[0], "open": True},
                "two": {"path": paths[1], "open": True},
            },
        )

        with pytest.raises(CliError) as exc:
            registry.active_vault_path()
        assert exc.value.err_type == "ambiguous_target"
        assert exc.value.details["open_vault_paths"] == paths

    def test_missing_registry_is_empty_and_has_no_active_vault(self):
        assert registry.registered_vaults() == []
        with pytest.raises(CliError) as exc:
            registry.active_vault_path()
        assert exc.value.err_type == "invalid_arguments"

    def test_unparseable_registry_is_empty(self, isolated_obsidian_registry):
        isolated_obsidian_registry.write_text("{not json", encoding="utf-8")

        assert registry.registered_vaults() == []

    def test_non_object_registry_is_empty(self, isolated_obsidian_registry):
        isolated_obsidian_registry.write_text("[]", encoding="utf-8")

        assert registry.registered_vaults() == []


class TestVaultNameForRoot:
    def test_exact_match(self, isolated_obsidian_registry, tmp_path):
        vault = tmp_path / "MyVault"
        vault.mkdir()
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(vault), "open": True}},
        )

        assert registry.vault_name_for_root(str(vault)) == "MyVault"

    def test_match_resolves_relative_paths_and_symlinks(
        self, isolated_obsidian_registry, tmp_path, monkeypatch
    ):
        vault = tmp_path / "Vault"
        vault.mkdir()
        alias = tmp_path / "Alias"
        alias.symlink_to(vault, target_is_directory=True)
        monkeypatch.chdir(tmp_path)
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": "Vault", "open": True}},
        )

        assert registry.vault_name_for_root(str(alias)) == "Vault"

    def test_match_accepts_case_variation_on_case_insensitive_filesystem(
        self, isolated_obsidian_registry, tmp_path
    ):
        vault = tmp_path / "CaseVault"
        vault.mkdir()
        swapped = vault.with_name(vault.name.swapcase())
        if not os.path.exists(swapped):
            pytest.skip("filesystem is case-sensitive")
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(vault), "open": True}},
        )

        assert registry.vault_name_for_root(str(swapped)) == "CaseVault"

    def test_unregistered_root_is_config_mismatch(
        self, isolated_obsidian_registry, tmp_path
    ):
        registered = tmp_path / "Registered"
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(registered), "open": True}},
        )

        with pytest.raises(CliError) as exc:
            registry.vault_name_for_root(str(tmp_path / "Other"))
        assert exc.value.err_type == "config_mismatch"
        assert str(registered) in exc.value.details["registered_paths"]

    def test_basename_collision_is_config_mismatch(
        self, isolated_obsidian_registry, tmp_path
    ):
        first = tmp_path / "one" / "Vault"
        second = tmp_path / "two" / "Vault"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        write_registry(
            isolated_obsidian_registry,
            {
                "one": {"path": str(first), "open": True},
                "two": {"path": str(second), "open": False},
            },
        )

        with pytest.raises(CliError) as exc:
            registry.vault_name_for_root(str(first))
        assert exc.value.err_type == "config_mismatch"
        assert exc.value.details["vault_name"] == "Vault"


class TestVaultPathForName:
    def test_exact_name_match(self, isolated_obsidian_registry, tmp_path):
        vault = tmp_path / "MyVault"
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(vault), "open": True}},
        )

        assert registry.vault_path_for_name("MyVault") == str(vault)

    def test_missing_name_is_config_mismatch(self):
        with pytest.raises(CliError) as exc:
            registry.vault_path_for_name("Missing")
        assert exc.value.err_type == "config_mismatch"

    def test_duplicate_name_is_ambiguous(
        self, isolated_obsidian_registry, tmp_path
    ):
        first = tmp_path / "one" / "Vault"
        second = tmp_path / "two" / "Vault"
        write_registry(
            isolated_obsidian_registry,
            {
                "one": {"path": str(first), "open": True},
                "two": {"path": str(second), "open": False},
            },
        )

        with pytest.raises(CliError) as exc:
            registry.vault_path_for_name("Vault")
        assert exc.value.err_type == "ambiguous_target"


def test_lint_uses_active_vault_when_root_is_unconfigured(
    capsys, isolated_config, isolated_obsidian_registry
):
    vault = isolated_config / "ActiveVault"
    vault.mkdir()
    (vault / "Note.md").write_text("body\n", encoding="utf-8")
    write_registry(
        isolated_obsidian_registry,
        {"one": {"path": str(vault), "open": True}},
    )

    code = cli.main(["lint"])
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    assert envelope["ok"] is True
    assert envelope["result"]["root"] == str(vault)
