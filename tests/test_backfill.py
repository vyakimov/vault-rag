"""Tests for tools/backfill.py."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import backfill  # noqa: E402

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def run(root: Path, apply: bool = False):
    return backfill.build_report(root, "*.md", apply)


def changes_for(report, rel):
    return {c["field"]: c for c in report["changes"] if c["path"] == rel}


def test_no_frontmatter_prepends_block(tmp_path):
    body = "hello\nbody without frontmatter"
    (tmp_path / "note.md").write_text(body, encoding="utf-8")
    run(tmp_path, apply=True)

    text = (tmp_path / "note.md").read_text()
    assert text.startswith("---\n")
    assert text.endswith(body)  # body byte-identical
    fields = re.findall(r"^(id|created|updated): (.+)$", text, re.MULTILINE)
    keys = [k for k, _ in fields]
    assert keys == ["id", "created", "updated"]
    note_id = dict(fields)["id"]
    assert ULID_RE.match(note_id)


def test_inserts_before_closing_fence_preserving_existing(tmp_path):
    raw = "---\ncustom: x\ntags: [a]\n---\nbody text\n"
    (tmp_path / "n.md").write_text(raw, encoding="utf-8")
    run(tmp_path, apply=True)

    text = (tmp_path / "n.md").read_text()
    assert "custom: x\n" in text
    assert "tags: [a]\n" in text
    # Existing keys precede the injected contract keys, all inside one block.
    order = [m.group(1) for m in re.finditer(r"^(custom|tags|id|created|updated):", text, re.MULTILINE)]
    assert order == ["custom", "tags", "id", "created", "updated"]
    assert text.endswith("body text\n")


def test_existing_created_untouched(tmp_path):
    raw = "---\ncreated: 2020-01-02T03:04:05Z\n---\nbody\n"
    (tmp_path / "n.md").write_text(raw, encoding="utf-8")
    report = run(tmp_path, apply=True)

    fields = changes_for(report, "n.md")
    assert "created" not in fields
    assert set(fields) == {"id", "updated"}
    assert "created: 2020-01-02T03:04:05Z" in (tmp_path / "n.md").read_text()


def test_legacy_uid_becomes_id(tmp_path):
    uid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    (tmp_path / "n.md").write_text(f"---\nuid: {uid}\n---\nbody\n", encoding="utf-8")
    report = run(tmp_path, apply=True)

    fields = changes_for(report, "n.md")
    assert fields["id"]["value"] == uid
    assert fields["id"]["source"] == "legacy_field"
    text = (tmp_path / "n.md").read_text()
    assert f"uid: {uid}\n" in text  # legacy line preserved
    assert f"id: {uid}\n" in text


def test_id_and_uid_disagree_goes_to_manual_review(tmp_path):
    raw = "---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nuid: 01BXXXXXXXXXXXXXXXXXXXXXXX\n---\nbody\n"
    (tmp_path / "n.md").write_text(raw, encoding="utf-8")
    report = run(tmp_path, apply=True)

    assert {m["path"] for m in report["manual_review"]} == {"n.md"}
    assert (tmp_path / "n.md").read_text() == raw  # untouched


def test_unparseable_created_goes_to_manual_review(tmp_path):
    raw = "---\ncreated: yesterday\n---\nbody\n"
    (tmp_path / "n.md").write_text(raw, encoding="utf-8")
    report = run(tmp_path, apply=True)

    reasons = {m["path"]: m["reason"] for m in report["manual_review"]}
    assert "n.md" in reasons
    assert (tmp_path / "n.md").read_text() == raw


def test_duplicate_ids_both_manual_review(tmp_path):
    dup = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    (tmp_path / "a.md").write_text(f"---\nid: {dup}\n---\nA\n", encoding="utf-8")
    (tmp_path / "b.md").write_text(f"---\nid: {dup}\n---\nB\n", encoding="utf-8")
    report = run(tmp_path, apply=True)
    assert {m["path"] for m in report["manual_review"]} == {"a.md", "b.md"}


def test_git_first_commit_source(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.co"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "n.md").write_text("body no frontmatter\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "n.md"], check=True)
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = "2022-03-04T05:06:07+00:00"
    env["GIT_COMMITTER_DATE"] = "2022-03-04T05:06:07+00:00"
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "add"], check=True, env=env)

    report = run(tmp_path, apply=False)
    created = changes_for(report, "n.md")["created"]
    assert created["source"] == "git_first_commit"
    # Timezone-robust: the recorded value must denote the commit instant,
    # regardless of the active TIMESTAMP_POLICY (UTC Z vs offset-local).
    from datetime import datetime, timezone
    assert datetime.fromisoformat(created["value"]).astimezone(timezone.utc) == datetime(
        2022, 3, 4, 5, 6, 7, tzinfo=timezone.utc
    )


def test_idempotent(tmp_path):
    (tmp_path / "n.md").write_text("body\n", encoding="utf-8")
    run(tmp_path, apply=True)
    second = run(tmp_path, apply=False)
    assert second["totals"]["changed"] == 0


def test_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "n.md"
    path.write_text("body\n", encoding="utf-8")
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime
    run(tmp_path, apply=False)
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime == before_mtime


def test_updated_clamped_to_created(tmp_path):
    # Legacy date is in the future -> created(2030) > updated(mtime, ~now) -> clamp.
    (tmp_path / "n.md").write_text("---\ndate: 2030-01-01\n---\nbody\n", encoding="utf-8")
    report = run(tmp_path, apply=False)
    fields = changes_for(report, "n.md")
    assert fields["created"]["source"] == "legacy_field"
    assert fields["updated"]["value"] == fields["created"]["value"]
    assert any("clamped" in w for w in fields["updated"]["warnings"])


def test_totals_are_consistent(tmp_path):
    # changed + unchanged + manual_review must equal scanned (no double counting).
    (tmp_path / "plain.md").write_text("body\n", encoding="utf-8")
    (tmp_path / "done.md").write_text(
        "---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ncreated: 2020-01-01T00:00:00Z\nupdated: 2020-01-02T00:00:00Z\n---\nx\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.md").write_text("---\ncreated: notadate\n---\nx\n", encoding="utf-8")
    report = run(tmp_path, apply=False)
    t = report["totals"]
    assert t["scanned"] == t["changed"] + t["skipped_unchanged"] + t["manual_review"]
    assert t["scanned"] == 3


def test_skips_reserved_dirs(tmp_path):
    (tmp_path / "keep.md").write_text("body\n", encoding="utf-8")
    for reserved in (".trash", ".obsidian", "Templates"):
        d = tmp_path / reserved
        d.mkdir()
        (d / "x.md").write_text("skip\n", encoding="utf-8")
    report = run(tmp_path, apply=True)
    touched = {c["path"] for c in report["changes"]} | {m["path"] for m in report["manual_review"]}
    assert touched == {"keep.md"}
