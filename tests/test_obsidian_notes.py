"""Tests for the note-mutation commands (no Obsidian; the backend is faked)."""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest
from conftest import write_config, write_registry

from vault_spider import cli
from vault_spider.envelope import CliError
from vault_spider.obsidian import backend as obsidian_backend


class FakeBackend:
    """Records backend calls and serves canned note content for reads."""

    def __init__(self, notes=None):
        self.notes = dict(notes or {})
        self.calls = []

    def _path(self, args):
        for arg in args:
            if arg.startswith("path="):
                return arg[len("path="):]
        return None

    def __call__(self, args, timeout=20.0):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "read":
            path = self._path(args)
            if path in self.notes:
                return self.notes[path]
            raise CliError("not_found", f"File {path} not found")
        if cmd == "create":
            return f"Created: {self._path(args)}"
        if cmd == "move":
            return "Moved: Inbox/Foo.md -> Research/Foo.md"
        if cmd == "rename":
            return "Renamed: Inbox/Foo.md -> Inbox/Better.md"
        if cmd == "eval" and "JSON.stringify({content})" in args[1]:
            match = re.search(r"getFileByPath\((\"(?:\\.|[^\"])*\")\)", args[1])
            path = json.loads(match.group(1)) if match else None
            if path not in self.notes:
                return "=> NOTFOUND"
            return "=> " + json.dumps({"content": self.notes[path]})
        # property:set, other eval, open
        return "=> OK"

    def mutating_calls(self):
        calls = []
        for call in self.calls:
            if call[0] in ("create", "property:set", "move", "rename"):
                calls.append(call)
            elif call[0] == "eval" and any(
                marker in call[1] for marker in ("app.vault.modify", "processFrontMatter")
            ):
                calls.append(call)
        return calls


@pytest.fixture(autouse=True)
def _isolated(isolated_config):
    """Shared isolated config plus a clean backend connection state per test."""
    obsidian_backend.configure()
    yield
    obsidian_backend.configure()


def enable_manage_updated(tmp_path):
    write_config(tmp_path, "obsidian:\n  manage_updated: true\n")


def run(argv, backend, capsys, monkeypatch):
    monkeypatch.setattr(obsidian_backend, "run", backend)
    code = cli.main(argv)
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


# -- backend layer -----------------------------------------------------------

class TestBackendLayer:
    def _fake_proc(self, stdout, returncode=0):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    def test_strips_noise_lines(self, monkeypatch):
        monkeypatch.setattr(obsidian_backend, "_resolve_binary", lambda: "/bin/true")
        monkeypatch.setattr(obsidian_backend.subprocess, "run",
                            lambda *a, **k: self._fake_proc("Loading updated app package v1\nHello\n"))
        assert obsidian_backend.run(["read", "path=x.md"]) == "Hello"

    def test_error_not_found(self, monkeypatch):
        monkeypatch.setattr(obsidian_backend, "_resolve_binary", lambda: "/bin/true")
        monkeypatch.setattr(obsidian_backend.subprocess, "run",
                            lambda *a, **k: self._fake_proc("Error: File x.md not found"))
        with pytest.raises(CliError) as exc:
            obsidian_backend.run(["read", "path=x.md"])
        assert exc.value.err_type == "not_found"

    def test_error_not_running(self, monkeypatch):
        monkeypatch.setattr(obsidian_backend, "_resolve_binary", lambda: "/bin/true")
        monkeypatch.setattr(obsidian_backend.subprocess, "run",
                            lambda *a, **k: self._fake_proc("Error: could not connect to vault"))
        with pytest.raises(CliError) as exc:
            obsidian_backend.run(["read", "path=x.md"])
        assert exc.value.err_type == "obsidian_not_running"

    def test_vault_not_found_without_error_prefix_is_config_mismatch(self, monkeypatch):
        obsidian_backend.configure(vault="MissingVault")
        monkeypatch.setattr(obsidian_backend, "_resolve_binary", lambda: "/bin/true")
        monkeypatch.setattr(
            obsidian_backend.subprocess,
            "run",
            lambda *a, **k: self._fake_proc("Vault not found.\n"),
        )

        with pytest.raises(CliError) as exc:
            obsidian_backend.run(["read", "path=x.md"])
        assert exc.value.err_type == "config_mismatch"
        assert exc.value.message == "Obsidian vault not found: MissingVault"

    def test_compare_and_write_rejects_an_atomic_conflict(self, monkeypatch):
        monkeypatch.setattr(obsidian_backend, "run", lambda args: "=> CONFLICT")

        with pytest.raises(CliError) as exc:
            obsidian_backend.compare_and_write_note("n.md", "before", "after")

        assert exc.value.err_type == "contract_violation"
        assert "changed since dry run" in exc.value.message

    def test_exact_snapshot_preserves_terminal_whitespace(self, monkeypatch):
        monkeypatch.setattr(
            obsidian_backend,
            "run",
            lambda args: '=> {"content":"body\\n\\n"}',
        )

        assert obsidian_backend.read_note_snapshot("n.md") == "body\n\n"


# -- create-note -------------------------------------------------------------

class TestCreateNote:
    def test_collision_precheck(self, capsys, monkeypatch):
        backend = FakeBackend({"Inbox/Foo.md": "---\nid: x\n---\nbody"})
        code, env = run(["create-note", "--path", "Inbox/Foo.md", "--content", "hi"],
                        backend, capsys, monkeypatch)
        assert code == 1
        assert env["error"]["type"] == "already_exists"
        assert not any(c[0] == "create" for c in backend.calls)

    def test_creates_with_frontmatter(self, capsys, monkeypatch):
        backend = FakeBackend()
        code, env = run(
            ["create-note", "--path", "Inbox/Foo.md", "--content", "line1\nline2",
             "--frontmatter", json.dumps({"id": "01ABC", "type": "idea"})],
            backend, capsys, monkeypatch,
        )
        assert code == 0 and env["ok"]
        create_call = next(c for c in backend.calls if c[0] == "create")
        content_arg = next(a for a in create_call if a.startswith("content="))
        assert "\\n" in content_arg  # real newlines escaped for the backend

    def test_dry_run_writes_nothing(self, capsys, monkeypatch):
        backend = FakeBackend()
        code, env = run(["create-note", "--path", "A.md", "--content", "x", "--dry-run"],
                        backend, capsys, monkeypatch)
        assert env["ok"] and env["meta"]["dry_run"] is True
        assert backend.mutating_calls() == []

    def test_auto_id_mints_identity(self, capsys, monkeypatch):
        import re
        from datetime import datetime

        backend = FakeBackend()
        code, env = run(["create-note", "--path", "A.md", "--content", "body",
                         "--auto-id", "--dry-run"],
                        backend, capsys, monkeypatch)
        assert env["ok"]
        text = env["result"]["text"]
        fields = dict(re.findall(r"^(id|created|updated): (.+)$", text, re.MULTILINE))
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", fields["id"])  # Crockford ULID
        assert fields["created"] == fields["updated"]
        assert datetime.fromisoformat(fields["created"]).tzinfo is not None

    def test_auto_id_uses_obsidian_native_datetimes(
        self, capsys, monkeypatch, isolated_config
    ):
        write_config(isolated_config, "timestamps:\n  policy: obsidian_local\n")
        backend = FakeBackend()

        _, env = run(
            ["create-note", "--path", "A.md", "--content", "body", "--auto-id", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )
        fields = dict(
            re.findall(r"^(created|updated): (.+)$", env["result"]["text"], re.MULTILINE)
        )

        assert fields["created"] == fields["updated"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", fields["created"])

    def test_auto_id_never_overwrites_explicit_frontmatter(self, capsys, monkeypatch):
        backend = FakeBackend()
        code, env = run(["create-note", "--path", "A.md", "--content", "body", "--auto-id",
                         "--frontmatter", json.dumps({"id": "01ABC", "type": "idea"}),
                         "--dry-run"],
                        backend, capsys, monkeypatch)
        assert env["ok"]
        text = env["result"]["text"]
        assert "id: 01ABC" in text
        assert "type: idea" in text
        assert "created: " in text and "updated: " in text


# -- edit-note ---------------------------------------------------------------

class TestEditNote:
    def test_dry_run_returns_unified_diff_and_guard_without_writing(
        self, capsys, monkeypatch
    ):
        raw = "---\nid: x\n---\nAlpha beta.\n"
        backend = FakeBackend({"n.md": raw})
        edits = json.dumps([{"old_text": "Alpha beta", "new_text": "Alpha gamma"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert env["meta"]["dry_run"] is True
        assert env["result"]["expected_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert env["result"]["proposed_sha256"] != env["result"]["expected_sha256"]
        assert env["result"]["diff"].startswith("--- a/n.md\n+++ b/n.md\n")
        assert "-Alpha beta." in env["result"]["diff"]
        assert "+Alpha gamma." in env["result"]["diff"]
        assert backend.mutating_calls() == []

    def test_dry_run_diff_includes_managed_updated_frontmatter(
        self, capsys, monkeypatch, isolated_config
    ):
        enable_manage_updated(isolated_config)
        raw = "---\nid: x\nupdated: 2026-01-01T00:00:00Z\n---\nAlpha beta.\n"
        backend = FakeBackend({"n.md": raw})
        edits = json.dumps([{"old_text": "Alpha beta", "new_text": "Alpha gamma"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        stamp = env["result"]["updated"]
        assert "-updated: 2026-01-01T00:00:00Z" in env["result"]["diff"]
        assert f"+updated: {stamp}" in env["result"]["diff"]
        proposed = raw.replace("2026-01-01T00:00:00Z", stamp).replace(
            "Alpha beta", "Alpha gamma"
        )
        assert env["result"]["proposed_sha256"] == hashlib.sha256(
            proposed.encode()
        ).hexdigest()
        assert backend.mutating_calls() == []

    def test_apply_diff_uses_timestamp_written_to_frontmatter(
        self, capsys, monkeypatch, isolated_config
    ):
        enable_manage_updated(isolated_config)
        raw = "---\nid: x\n---\nAlpha beta.\n"
        backend = FakeBackend({"n.md": raw})
        guard = hashlib.sha256(raw.encode()).hexdigest()
        edits = json.dumps([{"old_text": "Alpha beta", "new_text": "Alpha gamma"}])

        code, env = run(
            [
                "edit-note",
                "--path",
                "n.md",
                "--edits",
                edits,
                "--expected-sha256",
                guard,
            ],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        stamp = env["result"]["updated"]
        assert f"+updated: {stamp}" in env["result"]["diff"]
        updated_call = next(
            call for call in backend.calls
            if call[0] == "property:set" and "name=updated" in call
        )
        assert f"value={stamp}" in updated_call

    def test_plugin_owned_updated_is_not_rendered_as_a_change(self, capsys, monkeypatch):
        raw = "---\nupdated: plugin-owned\n---\nAlpha beta.\n"
        backend = FakeBackend({"n.md": raw})
        edits = json.dumps([{"old_text": "Alpha beta", "new_text": "Alpha gamma"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert "-updated:" not in env["result"]["diff"]
        assert "+updated:" not in env["result"]["diff"]

    def test_apply_requires_dry_run_guard(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "Alpha beta"})
        edits = json.dumps([{"old_text": "beta", "new_text": "gamma"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"
        assert "--expected-sha256 is required" in env["error"]["message"]
        assert backend.calls == []

    def test_apply_uses_compare_and_write_with_matching_guard(self, capsys, monkeypatch):
        raw = "Alpha beta"
        backend = FakeBackend({"n.md": raw})
        guard = hashlib.sha256(raw.encode()).hexdigest()
        edits = json.dumps([{"old_text": "beta", "new_text": "gamma"}])

        code, env = run(
            [
                "edit-note",
                "--path",
                "n.md",
                "--edits",
                edits,
                "--expected-sha256",
                guard,
            ],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert env["meta"]["dry_run"] is False
        eval_call = next(
            call for call in backend.calls
            if call[0] == "eval" and "app.vault.modify" in call[1]
        )
        assert "Alpha beta" in eval_call[1]
        assert "Alpha gamma" in eval_call[1]

    def test_changed_note_rejects_stale_guard_without_writing(self, capsys, monkeypatch):
        before = "Alpha beta"
        backend = FakeBackend({"n.md": "Alpha beta changed elsewhere"})
        stale_guard = hashlib.sha256(before.encode()).hexdigest()
        edits = json.dumps([{"old_text": "beta", "new_text": "gamma"}])

        code, env = run(
            [
                "edit-note",
                "--path",
                "n.md",
                "--edits",
                edits,
                "--expected-sha256",
                stale_guard,
            ],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "contract_violation"
        assert env["error"]["details"]["expected_sha256"] == stale_guard
        assert backend.mutating_calls() == []

    def test_repeated_text_requires_occurrence(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "one and one"})
        edits = json.dumps([{"old_text": "one", "new_text": "two"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "ambiguous_target"
        assert "occurs 2 times" in env["error"]["message"]

    def test_occurrence_selects_exact_match(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "one and one"})
        edits = json.dumps(
            [{"old_text": "one", "new_text": "two", "occurrence": 2}]
        )

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert "-one and one" in env["result"]["diff"]
        assert "+one and two" in env["result"]["diff"]

    def test_diff_renders_terminal_newline_change(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "body\n"})
        edits = json.dumps([{"old_text": "body\n", "new_text": "body"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert env["result"]["diff"]
        assert "\\ No newline at end of file" in env["result"]["diff"]

    def test_edits_are_body_only(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\ntitle: Alpha\n---\nBody"})
        edits = json.dumps([{"old_text": "Alpha", "new_text": "Beta"}])

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "not_found"

    def test_overlapping_edits_are_rejected(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "abcdef"})
        edits = json.dumps(
            [
                {"old_text": "abcd", "new_text": "one"},
                {"old_text": "cdef", "new_text": "two"},
            ]
        )

        code, env = run(
            ["edit-note", "--path", "n.md", "--edits", edits, "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"
        assert "overlap" in env["error"]["message"]


# -- merge-frontmatter -------------------------------------------------------

class TestMergeFrontmatter:
    def test_immutable_id_rejected(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: existing\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"id": "new"})],
                        backend, capsys, monkeypatch)
        assert code == 1 and env["error"]["type"] == "contract_violation"

    def test_immutable_provenance_rejected(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nprovenance: human\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch",
                         json.dumps({"provenance": "llm"})],
                        backend, capsys, monkeypatch)
        assert code == 1 and env["error"]["type"] == "contract_violation"

    def test_provenance_settable_when_absent(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\ntitle: T\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch",
                         json.dumps({"provenance": "reference"})],
                        backend, capsys, monkeypatch)
        assert code == 0
        assert "provenance" in env["result"]["fields_touched"]

    def test_empty_value_rejected(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\ntitle: T\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"type": ""})],
                        backend, capsys, monkeypatch)
        assert code == 1 and env["error"]["type"] == "invalid_arguments"

    def test_alias_union(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\naliases:\n  - A\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"aliases": ["A", "B"]})],
                        backend, capsys, monkeypatch)
        assert env["ok"] and env["result"]["changed"] is True
        eval_call = next(c for c in backend.calls if c[0] == "eval")
        assert '"A"' in eval_call[1] and '"B"' in eval_call[1]

    def test_noop_when_already_set(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\ntype: idea\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"type": "idea"})],
                        backend, capsys, monkeypatch)
        assert env["ok"] and env["result"]["changed"] is False
        assert backend.mutating_calls() == []

    def test_dry_run_no_mutation(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\ntitle: T\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch",
                         json.dumps({"type": "idea"}), "--dry-run"], backend, capsys, monkeypatch)
        assert env["meta"]["dry_run"] is True
        assert backend.mutating_calls() == []
        assert env["result"]["diffs"]["type"]["proposed"] == "idea"


# -- add-links ---------------------------------------------------------------

class TestAddLinks:
    def test_malformed_link_item_is_invalid_arguments(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "body"})

        code, env = run(
            ["add-links", "--path", "n.md", "--links", json.dumps(["Atlas"])],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"

    def test_anchor_resolution_and_idempotency(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: x\n---\nMeeting about Atlas today."})
        links = json.dumps([{"target": "Atlas", "anchor_text": "Atlas", "line": 1}])
        code, env = run(["add-links", "--path", "n.md", "--links", links],
                        backend, capsys, monkeypatch)
        assert env["ok"] and env["result"]["changed"] is True
        eval_call = next(c for c in backend.calls if c[0] == "eval")
        assert "[[Atlas]]" in eval_call[1]

        # Re-run against the now-linked body -> already, no change.
        backend2 = FakeBackend({"n.md": "---\nid: x\n---\nMeeting about [[Atlas]] today."})
        code, env = run(["add-links", "--path", "n.md", "--links", links],
                        backend2, capsys, monkeypatch)
        assert env["result"]["changed"] is False
        assert env["result"]["links"][0]["already"] is True

    def test_anchor_not_found(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: x\n---\nNo mention here."})
        links = json.dumps([{"target": "Ghost", "anchor_text": "Ghost"}])
        code, env = run(["add-links", "--path", "n.md", "--links", links],
                        backend, capsys, monkeypatch)
        assert env["result"]["links"][0]["applied"] is False
        assert env["result"]["links"][0]["reason"] == "anchor not found"

    def test_ignores_anchor_in_code_fence(self, capsys, monkeypatch):
        body = "---\nid: x\n---\ntext\n```\nAtlas\n```\n"
        backend = FakeBackend({"n.md": body})
        links = json.dumps([{"target": "Atlas", "anchor_text": "Atlas"}])
        code, env = run(["add-links", "--path", "n.md", "--links", links],
                        backend, capsys, monkeypatch)
        assert env["result"]["changed"] is False


# -- insert-related ----------------------------------------------------------

class TestInsertRelated:
    def test_empty_target_is_invalid_arguments(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "body"})

        code, env = run(
            ["insert-related", "--path", "n.md", "--targets", json.dumps([""])],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"

    def test_creates_section_and_dedupes(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: x\n---\nbody\n\n## Related\n- [[Existing]]\n"})
        code, env = run(["insert-related", "--path", "n.md", "--targets", json.dumps(["Existing", "New"])],
                        backend, capsys, monkeypatch)
        assert env["result"]["added"] == ["New"]
        assert env["result"]["already_present"] == ["Existing"]

    def test_appends_section_when_absent(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: x\n---\nbody\n"})
        code, env = run(["insert-related", "--path", "n.md", "--targets", json.dumps(["A"])],
                        backend, capsys, monkeypatch)
        assert env["result"]["changed"] is True
        eval_call = next(c for c in backend.calls if c[0] == "eval")
        assert "## Related" in eval_call[1] and "[[A]]" in eval_call[1]

    def test_ambiguous_multiple_related(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: x\n---\n## Related\n- [[A]]\n## Related\n- [[B]]\n"})
        code, env = run(["insert-related", "--path", "n.md", "--targets", json.dumps(["C"])],
                        backend, capsys, monkeypatch)
        assert code == 1 and env["error"]["type"] == "ambiguous_target"


# -- move / rename -----------------------------------------------------------

class TestMoveRename:
    def test_move_collision(self, capsys, monkeypatch):
        backend = FakeBackend({"Inbox/Foo.md": "x", "Research/Foo.md": "y"})
        code, env = run(["move-note", "--path", "Inbox/Foo.md", "--to", "Research/"],
                        backend, capsys, monkeypatch)
        assert code == 1 and env["error"]["type"] == "already_exists"

    def test_move_success_no_updated_bump(self, capsys, monkeypatch, isolated_config):
        enable_manage_updated(isolated_config)
        backend = FakeBackend({"Inbox/Foo.md": "x"})
        code, env = run(["move-note", "--path", "Inbox/Foo.md", "--to", "Research/"],
                        backend, capsys, monkeypatch)
        assert env["ok"] and env["result"]["links_updated_by"] == "obsidian"
        # arrow-form output is parsed to the post-arrow destination path.
        assert env["result"]["path_after"] == "Research/Foo.md"
        # move never patches updated even when manage_updated is on.
        assert not any(c[0] == "property:set" for c in backend.calls)

    def test_rename_parses_arrow_destination(self, capsys, monkeypatch):
        backend = FakeBackend({"Inbox/Foo.md": "x"})
        code, env = run(["rename-note", "--path", "Inbox/Foo.md", "--name", "Better"],
                        backend, capsys, monkeypatch)
        assert env["ok"] and env["result"]["path_after"] == "Inbox/Better.md"


# -- manage_updated ----------------------------------------------------------

class TestManageUpdated:
    def test_merge_bumps_updated_when_enabled(self, capsys, monkeypatch, isolated_config):
        enable_manage_updated(isolated_config)
        backend = FakeBackend({"n.md": "---\ntitle: T\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"type": "idea"})],
                        backend, capsys, monkeypatch)
        assert env["ok"]
        updated_calls = [c for c in backend.calls if c[0] == "property:set" and "name=updated" in c]
        assert len(updated_calls) == 1

    def test_merge_leaves_updated_alone_by_default(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\ntitle: T\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"type": "idea"})],
                        backend, capsys, monkeypatch)
        assert env["ok"]
        assert not any("name=updated" in c for c in backend.calls if c[0] == "property:set")


# -- envelope shape ----------------------------------------------------------

class TestEnvelope:
    def test_vault_path_traversal_is_rejected(self, capsys, monkeypatch):
        backend = FakeBackend()

        code, env = run(
            ["read-note", "--path", "../outside.md"], backend, capsys, monkeypatch
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"
        assert backend.calls == []

    def test_mutually_exclusive_read_views_return_json_error(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "body"})

        code, env = run(
            ["read-note", "--path", "n.md", "--body-only", "--frontmatter-only"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"
        assert backend.calls == []

    def test_success_shape(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: x\n---\nbody"})
        code, env = run(["read-note", "--path", "n.md"], backend, capsys, monkeypatch)
        assert set(["ok", "action", "result", "meta"]).issubset(env)
        assert env["action"] == "read-note"

    def test_failure_shape(self, capsys, monkeypatch):
        backend = FakeBackend()
        code, env = run(["read-note", "--path", "missing.md"], backend, capsys, monkeypatch)
        assert code == 1
        assert env["ok"] is False
        assert set(["type", "message", "details"]).issubset(env["error"])


# -- vault resolution guard --------------------------------------------------

class TestVaultResolution:
    def test_configured_root_drives_registered_vault_name(
        self, capsys, monkeypatch, isolated_config, isolated_obsidian_registry
    ):
        vault = isolated_config / "MyVault"
        vault.mkdir()
        write_config(isolated_config, f"vault:\n  root: {json.dumps(str(vault))}\n")
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(vault), "open": True}},
        )
        backend = FakeBackend()

        code, env = run(
            ["create-note", "--path", "A.md", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert obsidian_backend._STATE["vault"] == "MyVault"
        assert env["meta"]["vault"] == "MyVault"

    def test_unregistered_configured_root_fails_closed(
        self, capsys, monkeypatch, isolated_config
    ):
        root = isolated_config / "Unregistered"
        root.mkdir()
        write_config(isolated_config, f"vault:\n  root: {json.dumps(str(root))}\n")
        backend = FakeBackend()

        code, env = run(
            ["create-note", "--path", "A.md", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "config_mismatch"
        assert backend.calls == []

    def test_configured_name_and_root_mismatch_fails_closed(
        self, capsys, monkeypatch, isolated_config, isolated_obsidian_registry
    ):
        root = isolated_config / "ReadVault"
        other = isolated_config / "WriteVault"
        root.mkdir()
        other.mkdir()
        write_config(
            isolated_config,
            "vault:\n"
            f"  root: {json.dumps(str(root))}\n"
            "obsidian:\n"
            "  vault: WriteVault\n",
        )
        write_registry(
            isolated_obsidian_registry,
            {"other": {"path": str(other), "open": True}},
        )
        backend = FakeBackend()

        code, env = run(
            ["create-note", "--path", "A.md", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "config_mismatch"
        assert env["error"]["message"] == (
            "config.yaml obsidian.vault and vault.root point at different vaults"
        )
        assert backend.calls == []

    def test_explicit_vault_bypasses_registry_guard(
        self, capsys, monkeypatch, isolated_config
    ):
        root = isolated_config / "Unregistered"
        root.mkdir()
        write_config(isolated_config, f"vault:\n  root: {json.dumps(str(root))}\n")
        backend = FakeBackend()

        code, env = run(
            [
                "create-note",
                "--path",
                "A.md",
                "--vault",
                "DeliberateTarget",
                "--dry-run",
            ],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert obsidian_backend._STATE["vault"] == "DeliberateTarget"
        assert env["meta"]["vault"] == "DeliberateTarget"

    def test_explicit_vault_typo_is_rejected_when_registry_is_readable(
        self, capsys, monkeypatch, isolated_config, isolated_obsidian_registry
    ):
        vault = isolated_config / "MyVault"
        vault.mkdir()
        write_registry(
            isolated_obsidian_registry,
            {"one": {"path": str(vault), "open": True}},
        )
        backend = FakeBackend()

        code, env = run(
            ["create-note", "--path", "A.md", "--vault", "Valt", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "config_mismatch"
        assert backend.calls == []

    def test_explicit_vault_escape_hatch_works_without_registry(
        self, capsys, monkeypatch
    ):
        backend = FakeBackend()

        code, env = run(
            [
                "create-note",
                "--path",
                "A.md",
                "--vault",
                "Anything",
                "--dry-run",
            ],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert obsidian_backend._STATE["vault"] == "Anything"
        assert env["meta"]["vault"] == "Anything"

    def test_empty_explicit_vault_is_invalid_arguments(
        self, capsys, monkeypatch
    ):
        backend = FakeBackend()

        code, env = run(
            ["create-note", "--path", "A.md", "--vault", "", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 1
        assert env["error"]["type"] == "invalid_arguments"
        assert env["error"]["message"] == "--vault must not be empty"
        assert backend.calls == []

    def test_nothing_configured_uses_active_backend_target(
        self, capsys, monkeypatch
    ):
        backend = FakeBackend()

        code, env = run(
            ["create-note", "--path", "A.md", "--dry-run"],
            backend,
            capsys,
            monkeypatch,
        )

        assert code == 0 and env["ok"]
        assert obsidian_backend._STATE["vault"] is None
        assert env["meta"]["vault"] == "active"
