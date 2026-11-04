"""Ingest roundtrip on synthetic fixtures (hermetic: tmp DB, tmp workspace).

Exercises all three adapters for real — a fake OpenCode session SQLite, a real
tmp git repo and tmp docs — through ``ingest.runner.run_all``. No network:
embeddings are disabled globally in conftest.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from neobrain import config, db
from neobrain.ingest import docs as docs_adapter
from neobrain.ingest import gitrepo, opencode, runner

TS = 1_756_000_000_000  # a fixed epoch-ms so event ids are stable across runs
XCODE_REMINDER = (
    "<system-reminder>## Xcode\n\nPreviously verified results may be stale.\n"
    "</system-reminder>\n"
)
XCODE_TREE = (
    "Project structure (these are Xcode workspace-relative paths and do not "
    "correspond to filesystem paths. Use XcodeRead to interact with these files):\n"
    "Demo/App/DemoApp.swift\n"
    "Demo/Screens/HomeScreen.swift\n\n"
    "Package dependencies: ExamplePackage\n\n"
)
XCODE_NO_SELECTION = (
    "The user is currently inside this file: Demo/Tests/HomeTests.swift\n\n"
    "The user has no code selected.\n\n"
)


def _xcode_selection(first_line: int, last_line: int, code: str) -> str:
    return (
        "The user is currently inside this file: Demo/Screens/HomeScreen.swift\n\n"
        "The user has selected the following code from that file "
        f"(lines {first_line}-{last_line}):\n{code}\n\n"
    )


def _build_opencode_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session_v2 (
          id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT,
          agent TEXT, model TEXT, cost REAL, time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE session_message (
          id TEXT PRIMARY KEY, session_id TEXT, type TEXT, time_created INTEGER,
          data TEXT, seq INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES ('s1','p1','/home/x','Fix the DNS','build','m',0.0,?,?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO session_message VALUES ('m1','s1','user',?,?,1)",
        (TS, json.dumps({"text": "please fix the dns", "time": {"created": TS}})),
    )
    assistant = {
        "model": {"id": "deepseek-v4.1-flash"},
        "time": {"created": TS + 10},
        "content": [
            {
                "type": "tool",
                "id": "p1",
                "name": "shell",
                "state": {
                    "status": "completed",
                    "input": {"command": "systemctl restart unbound"},
                    "content": [{"type": "text", "text": "ok"}],
                },
                "time": {"created": TS + 10},
            },
            {"type": "text", "text": "We decided to fix the DNS resolver."},
            {"type": "reasoning", "text": "long reasoning " * 12},
        ],
    }
    conn.execute(
        "INSERT INTO session_message VALUES ('m2','s1','assistant',?,?,2)",
        (TS + 10, json.dumps(assistant)),
    )
    conn.commit()
    conn.close()


def _build_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "README.md"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "fix: add readme"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / "2026-09-29.md").write_text(
        "# Day\n\n## Decision: use PostgreSQL\nBecause it is boring and reliable.\n",
        encoding="utf-8",
    )
    (ws / "INFRA_TEST.md").write_text(
        "# Infra\n\n## Root cause: DNAT was wrong\nThe port mapping was inverted.\n",
        encoding="utf-8",
    )
    (ws / "DREAMS.md").write_text(
        "# Dreams\n\n<!-- neobrain:dreaming:diary:start -->\n"
        "\n---\n*September 28, 2026 at 2:00 AM*\nA quiet night.\n\n"
        "<!-- neobrain:dreaming:diary:end -->\n",
        encoding="utf-8",
    )
    (ws / "memory" / "dreaming" / "light").mkdir(parents=True)
    (ws / "memory" / "dreaming" / "light" / "2026-09-28.md").write_text(
        "# Light Sleep\n\n- shipped ingest\n", encoding="utf-8"
    )
    _build_opencode_db(tmp_path / "opencode.db")
    _build_git_repo(ws / "repo")

    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(config, "OPENCODE_DB", tmp_path / "opencode.db")
    monkeypatch.setattr(config, "KNOWN_REPOS", ["repo"])
    return ws


def test_iter_events_xcode_wrapper_keeps_only_user_message(workspace):
    path = config.OPENCODE_DB
    assert path is not None
    conn = sqlite3.connect(path)
    wrapped = XCODE_REMINDER + XCODE_TREE + XCODE_NO_SELECTION + "when do we ship adapters?"
    conn.execute(
        "INSERT INTO session_message VALUES ('m3','s1','user',?,?,3)",
        (TS + 20, json.dumps({"text": wrapped, "time": {"created": TS + 20}})),
    )
    conn.commit()
    conn.close()

    events, _ = opencode.iter_events()
    event = next(event for event in events if event.source_ref == "s1:m3:user")

    assert event.title == "when do we ship adapters?"
    assert event.detail == "when do we ship adapters?"
    assert event.raw == {"text": "when do we ship adapters?"}


def test_iter_events_xcode_selection_keeps_multiline_user_message(workspace):
    path = config.OPENCODE_DB
    assert path is not None
    conn = sqlite3.connect(path)
    code = "func example() {\n\n}\nprint(1)"
    user_message = "first line\n\nsecond paragraph"
    wrapped = XCODE_TREE + _xcode_selection(10, 13, code) + user_message
    conn.execute(
        "INSERT INTO session_message VALUES ('m3','s1','user',?,?,3)",
        (TS + 20, json.dumps({"text": wrapped, "time": {"created": TS + 20}})),
    )
    conn.commit()
    conn.close()

    events, _ = opencode.iter_events()
    event = next(event for event in events if event.source_ref == "s1:m3:user")

    assert event.title == "first line"
    assert event.detail == user_message
    assert event.raw == {"text": user_message}


def test_iter_events_unknown_xcode_footer_preserves_remaining_text(workspace):
    path = config.OPENCODE_DB
    assert path is not None
    conn = sqlite3.connect(path)
    wrapped = XCODE_TREE + "Some future footer line\n\nwhen do we ship adapters?"
    conn.execute(
        "INSERT INTO session_message VALUES ('m3','s1','user',?,?,3)",
        (TS + 20, json.dumps({"text": wrapped, "time": {"created": TS + 20}})),
    )
    conn.commit()
    conn.close()

    events, _ = opencode.iter_events()
    event = next(event for event in events if event.source_ref == "s1:m3:user")

    assert "Project structure" not in event.detail
    assert "Some future footer line" in event.detail
    assert event.detail.endswith("when do we ship adapters?")


def test_strip_host_scaffolding_drops_other_agents_block():
    others = (
        "Other agents are currently working in this workspace. "
        "Files they touch may change while you work:\n"
        "- Refactor the demo settings screen…\n- Fetch the ticket…\n\n\n"
    )
    message = "when do we ship adapters?"
    assert opencode.strip_host_scaffolding(XCODE_TREE + XCODE_NO_SELECTION + others + message) == message
    assert opencode.strip_host_scaffolding(XCODE_TREE + "The user has no file currently open.\n\n" + others + message) == message
    # A user sentence that merely resembles the block is kept.
    mention = "Other agents are currently working in this workspace, right?"
    assert opencode.strip_host_scaffolding(XCODE_TREE + XCODE_NO_SELECTION + mention) == mention


def test_strip_host_scaffolding_preserves_plain_and_unverified_text():
    message = "explain the Project structure idea\nplease"
    unverified = XCODE_TREE + _xcode_selection(1, 5, "only one line") + message
    incomplete_tree = (
        "Project structure (these are Xcode workspace-relative paths):\n"
        "Demo/App/DemoApp.swift\n"
        "when do we ship adapters?"
    )

    assert opencode.strip_host_scaffolding(message) == message
    assert opencode.strip_host_scaffolding(XCODE_TREE + "The user has no file currently open.\n\n" + message) == message
    assert opencode.strip_host_scaffolding(incomplete_tree) == incomplete_tree
    cleaned = opencode.strip_host_scaffolding(unverified)
    assert cleaned == "only one line\n\n" + message


def test_second_run_repairs_existing_xcode_event(conn, workspace):
    path = config.OPENCODE_DB
    assert path is not None
    source = sqlite3.connect(path)
    wrapped = XCODE_TREE + XCODE_NO_SELECTION + "please fix the dns"
    source.execute(
        "UPDATE session_message SET data = ? WHERE id = 'm1'",
        (json.dumps({"text": wrapped, "time": {"created": TS}}),),
    )
    source.commit()
    source.close()

    runner.run_all(conn, sources=("opencode",))
    event_id = conn.execute(
        "SELECT id FROM events WHERE session_id = 's1' AND lane = 'request' AND ts = ?",
        (TS,),
    ).fetchone()["id"]
    conn.execute(
        "UPDATE events SET title = 'Project structure', detail = 'legacy wrapper' WHERE id = ?",
        (event_id,),
    )
    conn.commit()

    summary = runner.run_all(conn, sources=("opencode",))
    event = conn.execute(
        "SELECT title, detail FROM events WHERE id = ?", (event_id,)
    ).fetchone()

    assert summary["opencode"]["added"] == 0
    assert summary["opencode"]["updated"] > 0
    assert event["title"] == "please fix the dns"
    assert event["detail"] == "please fix the dns"


def test_run_all_ingests_every_source(conn, workspace):
    summary = runner.run_all(conn)

    assert set(summary) == {"opencode", "git", "docs"}
    assert summary["opencode"]["added"] > 0
    assert summary["git"]["added"] == 1
    assert summary["docs"]["added"] > 0
    for bucket in summary.values():
        assert bucket["errors"] == 0

    stats = db.stats(conn)
    assert stats["by_source"]["opencode"] >= 3  # request + shell tool + decision
    assert stats["by_source"]["git"] == 1
    assert stats["by_source"]["docs"] >= 2
    assert stats["sessions"] == 1
    assert stats["doc_versions"] >= 1


def test_run_all_records_per_source_ingest_runs(conn, workspace):
    runner.run_all(conn)
    rows = list(conn.execute(
        "SELECT source, added, updated, errors FROM ingest_runs ORDER BY id"
    ))
    by_source = {r["source"]: r for r in rows}
    assert set(by_source) == {"opencode", "git", "docs"}
    assert by_source["git"]["added"] == 1
    assert by_source["git"]["updated"] == 0
    assert by_source["opencode"]["added"] > 0
    for r in rows:
        assert r["errors"] == 0


def test_ingest_run_rows_open_before_the_work(conn, workspace):
    runner.run_all(conn)
    rows = list(conn.execute(
        "SELECT source, started_at, finished_at FROM ingest_runs ORDER BY id"
    ))
    by_source = {r["source"]: r for r in rows}
    assert set(by_source) == {"opencode", "git", "docs"}
    for r in rows:
        assert r["started_at"] <= r["finished_at"]
    # The last row opened (git's, in the adapter phase) must already exist by
    # the time the first row is finished (after the shared upsert), i.e. the
    # rows span the real work window instead of only the bookkeeping loop.
    assert by_source["git"]["started_at"] <= by_source["opencode"]["finished_at"]


def test_second_run_updates_instead_of_adding(conn, workspace):
    first = runner.run_all(conn)
    second = runner.run_all(conn)
    for source in first:
        assert second[source]["added"] == 0
        assert second[source]["updated"] == first[source]["added"]


def test_run_all_sources_subset(conn, workspace):
    summary = runner.run_all(conn, sources=("git",))
    assert set(summary) == {"git"}
    assert summary["git"]["added"] == 1


def test_missing_opencode_db_skips_cleanly(conn, tmp_path, monkeypatch):
    # OPENCODE_DB unset (None) -> the source reports zeroes and does not raise.
    monkeypatch.setattr(config, "OPENCODE_DB", None)
    monkeypatch.setattr(config, "WORKSPACE", None)
    summary = runner.run_all(conn, sources=("opencode",))
    assert summary == {"opencode": {"added": 0, "updated": 0, "errors": 0}}


def test_broken_adapter_is_counted_not_raised(conn, workspace, monkeypatch):
    def boom():
        raise RuntimeError("session db corrupt")

    monkeypatch.setattr(opencode, "iter_events", boom)
    summary = runner.run_all(conn, sources=("opencode", "git"))
    assert summary["opencode"] == {"added": 0, "updated": 0, "errors": 1}
    assert summary["git"]["added"] == 1  # the other source still ran

    row = conn.execute(
        "SELECT errors FROM ingest_runs WHERE source='opencode' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["errors"] == 1


def test_opencode_adapter_maps_messages(conn, workspace):
    events, meta = opencode.iter_events()
    categories = {e.category for e in events}
    assert {"request", "shell", "statement", "rationale"} <= categories
    assert len(meta["sessions"]) == 1
    # rationale -> action/decision links were collected
    assert any(kind == "rationale" for _f, _t, kind in meta["links"])


def test_docs_adapter_finds_tracked_files(conn, workspace):
    events, versions = docs_adapter.iter_events()
    paths = {e.refs.get("path") for e in events}
    assert str(workspace / "memory" / "2026-09-29.md") in paths
    assert any(v["doc_path"].endswith("INFRA_TEST.md") for v in versions)


def test_gitrepo_adapter_reads_repo(conn, workspace):
    events = gitrepo.iter_events()
    assert len(events) == 1
    assert events[0].category == "commit"
    assert events[0].title.startswith("fix:")
