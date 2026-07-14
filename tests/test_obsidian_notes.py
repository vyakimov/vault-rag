"""Tests for the note-mutation commands (no Obsidian; the backend is faked)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import write_config

from vault_rag import cli
from vault_rag.envelope import CliError
from vault_rag.obsidian import backend as obsidian_backend


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
        # property:set, eval, open
        return "=> OK"

    def mutating_calls(self):
        return [c for c in self.calls if c[0] in ("create", "property:set", "eval", "move", "rename")]


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


# -- merge-frontmatter -------------------------------------------------------

class TestMergeFrontmatter:
    def test_immutable_id_rejected(self, capsys, monkeypatch):
        backend = FakeBackend({"n.md": "---\nid: existing\n---\nbody"})
        code, env = run(["merge-frontmatter", "--path", "n.md", "--patch", json.dumps({"id": "new"})],
                        backend, capsys, monkeypatch)
        assert code == 1 and env["error"]["type"] == "contract_violation"

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
