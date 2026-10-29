"""Tests for neobrain.sandbox (Counterfactual Dream-Sandbox).

Derived strictly from specs/dream-sandbox-brief.md (AC-1 through AC-6).
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neobrain import cli, config, db, embeddings, life, mind, rank, settings_store
from neobrain.api import app


def _sandbox():
    from neobrain import sandbox  # type: ignore[attr-defined]

    return sandbox


NOW = datetime(2026, 9, 30, 2, 0)  # 02:00 -> _dream_date resolves to 2026-09-29
DATE = "2026-09-29"


class FakeRuntime:
    def __init__(self, replies=None, error: Exception | None = None):
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, **kwargs})
        if self.error:
            raise self.error
        if self.replies:
            item = self.replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / f"{DATE}.md").write_text(
        "## Open Question\nDoes SQLite WAL mode with synchronous=NORMAL preserve durability on crash?\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKSPACE", ws)
    return ws


# ===========================================================================
# AC-1: Static AST Safety Gate (sandbox.validate_code)
# ===========================================================================


def test_ac1_validate_code_rejects_empty_and_syntax_errors():
    sb = _sandbox()
    for bad in ("", "   \n\t  ", "def broken(:\n    pass"):
        ok, reason = sb.validate_code(bad)
        assert ok is False
        assert isinstance(reason, str) and len(reason) > 0


@pytest.mark.parametrize(
    "mod",
    [
        "socket",
        "urllib",
        "urllib.request",
        "http",
        "http.client",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "subprocess",
        "shutil",
        "ctypes",
        "multiprocessing",
        "signal",
        "pty",
        "fcntl",
        "resource",
        "webbrowser",
        "ensuredir",
        "pip",
        "venv",
    ],
)
def test_ac1_validate_code_rejects_forbidden_imports(mod):
    sb = _sandbox()
    ok1, reason1 = sb.validate_code(f"import {mod}\nx = 1\n")
    assert ok1 is False
    assert reason1 is not None and mod.split(".")[0] in reason1

    top = mod.split(".")[0]
    ok2, reason2 = sb.validate_code(f"from {mod} import something\n")
    assert ok2 is False
    assert reason2 is not None and top in reason2


@pytest.mark.parametrize(
    "snippet",
    [
        "eval('1 + 1')",
        "exec('x = 1')",
        "compile('x = 1', '<str>', 'exec')",
        "__import__('os')",
        "input('prompt:')",
        "breakpoint()",
        "obj.__class__",
        "obj.__subclasses__()",
        "x = ().__class__.__bases__",
    ],
)
def test_ac1_validate_code_rejects_forbidden_builtins_and_dunders(snippet):
    sb = _sandbox()
    ok, reason = sb.validate_code(snippet)
    assert ok is False
    assert isinstance(reason, str) and len(reason) > 0


@pytest.mark.parametrize(
    "call_expr",
    [
        "import os\nos.system('id')",
        "import os\nos.popen('id')",
        "import os\nos.spawnl(os.P_WAIT, '/bin/ls')",
        "import os\nos.spawnp(os.P_WAIT, 'ls')",
        "import os\nos.execl('/bin/ls', 'ls')",
        "import os\nos.execvp('ls', ['ls'])",
        "import os\nos.fork()",
        "import os\nos.kill(1, 9)",
        "import os\nos.killpg(1, 9)",
        "import os\nos.remove('/tmp/a')",
        "import os\nos.unlink('/tmp/a')",
        "import os\nos.rmdir('/tmp/a')",
        "import os\nos.removedirs('/tmp/a')",
        "import os\nos.rename('/tmp/a', '/tmp/b')",
        "import os\nos.replace('/tmp/a', '/tmp/b')",
        "import os\nos.chmod('/tmp/a', 0o777)",
        "import os\nos.chown('/tmp/a', 0, 0)",
        "import os\nos.chroot('/tmp')",
        "import os\nos.symlink('/tmp/a', '/tmp/b')",
        "import os\nos.link('/tmp/a', '/tmp/b')",
    ],
)
def test_ac1_validate_code_rejects_dangerous_os_mutations(call_expr):
    sb = _sandbox()
    ok, reason = sb.validate_code(call_expr)
    assert ok is False
    assert isinstance(reason, str) and len(reason) > 0


def test_ac1_validate_code_allows_safe_stdlib_modules():
    sb = _sandbox()
    safe_script = """
import math, statistics, time, json, re, collections, itertools, functools
import hashlib, random, sqlite3, dataclasses, typing, decimal, fractions
import heapq, bisect, array, struct

data = [math.sqrt(x) for x in range(1, 10)]
mean_val = statistics.mean(data)
print(json.dumps({"verdict": "confirmed", "finding": f"mean={mean_val:.2f}", "metrics": {"n": len(data)}}))
"""
    ok, reason = sb.validate_code(safe_script)
    assert ok is True
    assert reason is None


# ===========================================================================
# AC-2: Hermetic Execution Runner (sandbox.execute_experiment)
# ===========================================================================


def test_ac2_execute_experiment_rejects_unsafe_code_without_spawning(monkeypatch):
    sb = _sandbox()
    import subprocess

    spawned = []
    orig_run = subprocess.run

    def spy_run(*args, **kwargs):
        spawned.append(args)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    res = sb.execute_experiment("import socket\nsocket.socket()\n")
    assert res["ok"] is False
    assert res["status"] == "rejected"
    assert res["returncode"] is None
    assert res["stdout"] == ""
    assert res["stderr"] == ""
    assert res["duration_ms"] == 0
    assert res["result"] is None
    assert isinstance(res["error"], str) and "socket" in res["error"]
    assert spawned == []


def test_ac2_execute_experiment_runs_isolated_and_cleans_up_tempdir(monkeypatch):
    sb = _sandbox()
    created_dirs: list[Path] = []
    orig_tempdir = tempfile.TemporaryDirectory

    class TrackingTempDir:
        def __init__(self, *args, **kwargs):
            self._inner = orig_tempdir(*args, **kwargs)
            created_dirs.append(Path(self._inner.name))

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._inner.__exit__(exc_type, exc_val, exc_tb)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", TrackingTempDir)

    code = """
import json, os, sqlite3, sys

# Create a local sqlite file inside tmp_dir to verify cwd is writable and cleaned up
sqlite3.connect("probe.db").close()

print("leading log line before JSON")
print(json.dumps({
    "verdict": "confirmed",
    "finding": "Isolated execution succeeded with scrubbed env.",
    "metrics": {
        "isolated": sys.flags.isolated,
        "has_secret": "SECRET_LEAK_TEST" in os.environ,
        "home_matches_cwd": os.environ.get("HOME") == os.getcwd(),
    }
}))
"""
    monkeypatch.setenv("SECRET_LEAK_TEST", "should-not-leak")
    res = sb.execute_experiment(code, timeout=10.0)
    assert res["ok"] is True
    assert res["status"] == "ok"
    assert res["returncode"] == 0
    assert res["duration_ms"] >= 0
    assert isinstance(res["result"], dict)
    assert res["result"]["verdict"] == "confirmed"
    assert "Isolated execution succeeded" in res["result"]["finding"]
    assert res["result"]["metrics"]["isolated"] == 1
    assert res["result"]["metrics"]["has_secret"] is False
    assert res["result"]["metrics"]["home_matches_cwd"] is True

    assert len(created_dirs) == 1
    assert created_dirs[0].name.startswith("neobrain-sandbox-")
    assert not created_dirs[0].exists()


def test_ac2_execute_experiment_enforces_timeout():
    sb = _sandbox()
    code = "import time\ntime.sleep(5)\n"
    res = sb.execute_experiment(code, timeout=0.2)
    assert res["ok"] is False
    assert res["status"] == "timeout"
    assert res["result"] is None
    assert isinstance(res["error"], str) and len(res["error"]) > 0


def test_ac2_execute_experiment_handles_nonzero_exit():
    sb = _sandbox()
    code = "import sys\nsys.stderr.write('assertion failed in experiment\\n')\nsys.exit(2)\n"
    res = sb.execute_experiment(code, timeout=5.0)
    assert res["ok"] is False
    assert res["status"] == "failed"
    assert res["returncode"] == 2
    assert "assertion failed in experiment" in res["stderr"]
    assert res["result"] is None


# ===========================================================================
# AC-3: End-to-End Nightly Sandbox Pass & Memory Promotion (sandbox.run)
# ===========================================================================


def test_ac3_sandbox_run_executes_writes_reports_promotes_lesson_and_links(conn, workspace):
    sb = _sandbox()
    seed = mind.remember(
        conn,
        "Hypothesis: SQLite WAL mode readers never block writers on the same table.",
        atype="open",
        hubs=["sqlite"],
        source="test",
    )
    seed_id = seed["id"]

    exp_code = """
import json, sqlite3
con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE t(x INT)")
con.execute("INSERT INTO t VALUES (42)")
val = con.execute("SELECT x FROM t").fetchone()[0]
print(json.dumps({
    "verdict": "confirmed",
    "finding": "In-memory SQLite table round-trips integer 42 deterministically.",
    "metrics": {"val": val}
}))
"""
    llm_reply = json.dumps(
        {
            "question": "Does SQLite round-trip integer 42 in an isolated connection?",
            "hypothesis": "Selecting the inserted row returns 42.",
            "hub": "sqlite",
            "prior_atom_id": seed_id,
            "code": exp_code,
        }
    )
    rt = FakeRuntime([llm_reply])
    out = sb.run(conn, rt, now=NOW)

    assert out["status"] == "ok"
    assert out["date"] == DATE
    assert out["verdict"] == "confirmed"
    assert out.get("atom_id") and str(out["atom_id"]).startswith("m_")

    # Check LLM call used tier "strong" and json_mode=True
    assert len(rt.calls) == 1
    assert rt.calls[0]["model"] == "strong"
    assert rt.calls[0]["json_mode"] is True

    # Check files written under <dream_root>/sandbox/<date>.json and .md
    sandbox_dir = workspace / "memory" / "dreaming" / "sandbox"
    json_file = sandbox_dir / f"{DATE}.json"
    md_file = sandbox_dir / f"{DATE}.md"
    assert json_file.exists()
    assert md_file.exists()

    record = json.loads(json_file.read_text(encoding="utf-8"))
    assert record["question"] == "Does SQLite round-trip integer 42 in an isolated connection?"
    assert record["hypothesis"] == "Selecting the inserted row returns 42."
    assert record["hub"] == "sqlite"
    assert record["prior_atom_id"] == seed_id
    assert record["status"] == "ok"
    assert record["result"]["verdict"] == "confirmed"

    md_text = md_file.read_text(encoding="utf-8")
    assert "# Dream Sandbox" in md_text
    assert "confirmed" in md_text
    assert "In-memory SQLite table round-trips integer 42" in md_text

    # Check lesson atom stored with source="memory/dreaming/sandbox/2026-09-29.md" and linked derived-from seed_id
    atom_id = out["atom_id"]
    row = conn.execute(
        "SELECT type, text, source, hub FROM m_atoms WHERE id=?", (atom_id,)
    ).fetchone()
    assert row is not None
    assert row["type"] == "lesson"
    assert "In-memory SQLite table round-trips integer 42" in row["text"]
    assert row["source"] == f"memory/dreaming/sandbox/{DATE}.md"
    assert row["hub"] == "sqlite"

    edge = conn.execute(
        "SELECT 1 FROM m_edges WHERE source=? AND target=? AND type='derived-from'",
        (atom_id, seed_id),
    ).fetchone()
    assert edge is not None

    # Check event emitted
    ev = conn.execute(
        "SELECT source, lane, category, actor, title, detail FROM events WHERE title='life: dream sandbox'"
    ).fetchone()
    assert ev is not None
    assert ev["source"] == "life"
    assert ev["lane"] == "notice"
    assert ev["category"] == "life"
    assert ev["actor"] == "agent:neobrain"

    # Second run on same date is skipped via once-per-day marker
    second = sb.run(conn, rt, now=NOW)
    assert second == {"status": "skipped", "reason": "already-ran-tonight", "date": DATE}
    assert len(rt.calls) == 1


def test_ac3_sandbox_run_inconclusive_does_not_promote_lesson_and_survives_failures(conn, workspace):
    sb = _sandbox()
    exp_code = """
import json
print(json.dumps({
    "verdict": "inconclusive",
    "finding": "Not enough variance to conclude.",
    "metrics": {"samples": 2}
}))
"""
    llm_reply = json.dumps(
        {
            "question": "Is sample size 2 enough?",
            "hypothesis": "Two samples suffice.",
            "hub": "stats",
            "prior_atom_id": None,
            "code": exp_code,
        }
    )
    # First call fails on "strong" tier, falls back to "cheap" tier
    rt = FakeRuntime([RuntimeError("strong tier timeout"), llm_reply])
    out = sb.run(conn, rt, now=NOW)

    assert out["status"] == "ok"
    assert out["verdict"] == "inconclusive"
    assert out.get("atom_id") is None
    assert [c["model"] for c in rt.calls] == ["strong", "cheap"]

    lessons = conn.execute("SELECT COUNT(*) FROM m_atoms WHERE type='lesson'").fetchone()[0]
    assert lessons == 0


def test_ac3_sandbox_run_never_raises_when_llm_fails(conn, workspace):
    sb = _sandbox()
    rt = FakeRuntime(error=RuntimeError("llm completely unreachable"))
    out = sb.run(conn, rt, now=NOW)
    assert out["status"] in ("error", "failed")
    ev = conn.execute("SELECT title, severity FROM events WHERE title='life: dream sandbox'").fetchone()
    assert ev is not None


# ===========================================================================
# AC-4: Life Loop, Settings, CLI & API Integration
# ===========================================================================


def test_ac4_settings_and_registry_include_sandbox_knobs(conn, monkeypatch):
    # Ensure sandbox_enabled and sandbox_timeout_seconds exist on Settings and REGISTRY
    defaults = config.Settings()
    assert hasattr(defaults, "sandbox_enabled")
    assert defaults.sandbox_enabled == "1"
    assert hasattr(defaults, "sandbox_timeout_seconds")
    assert defaults.sandbox_timeout_seconds == 10

    by_key = {s.key: s for s in settings_store.REGISTRY}
    assert "sandbox_enabled" in by_key
    assert by_key["sandbox_enabled"].label == "Dream sandbox"
    assert by_key["sandbox_enabled"].kind == "bool"
    assert by_key["sandbox_enabled"].section == "features"

    assert "sandbox_timeout_seconds" in by_key
    assert by_key["sandbox_timeout_seconds"].label == "Sandbox timeout"
    assert by_key["sandbox_timeout_seconds"].kind == "int"
    assert by_key["sandbox_timeout_seconds"].section == "schedule"
    assert by_key["sandbox_timeout_seconds"].minimum == 1
    assert by_key["sandbox_timeout_seconds"].maximum == 60
    assert by_key["sandbox_timeout_seconds"].suffix == "s"

    snap = settings_store.snapshot(conn)
    snap_keys = {f["key"] for sec in snap["sections"] for f in sec["fields"]}
    assert {"sandbox_enabled", "sandbox_timeout_seconds"} <= snap_keys


def test_ac4_life_loop_runs_sandbox_at_dream_hour_once(tmp_path, monkeypatch):
    sb = _sandbox()
    monkeypatch.setattr(embeddings, "embed_atom", lambda conn, atom_id: False)
    monkeypatch.setattr(life, "PROBES", (("llm", lambda conn=None: (True, "ok")),))
    monkeypatch.setattr(config.settings, "dreams_enabled", "0")
    monkeypatch.setattr(config.settings, "soul_reflect_enabled", "0")
    monkeypatch.setattr(config.settings, "sandbox_enabled", "1")

    calls = []

    def fake_sandbox_run(conn, runtime, *, now=None):
        calls.append(now)
        return {"status": "ok", "date": DATE, "verdict": "confirmed"}

    monkeypatch.setattr(sb, "run", fake_sandbox_run)

    current_dt = [NOW]
    loop = life.LifeLoop(
        db_path=tmp_path / "neobrain.db",
        clock=lambda: current_dt[0],
        runtime=FakeRuntime(),
    )
    try:
        first = loop.tick()
        assert "sandbox" in first["ran"]
        assert len(calls) == 1

        current_dt[0] = NOW + timedelta(minutes=20)
        second = loop.tick()
        assert "sandbox" not in second["ran"]
        assert len(calls) == 1
    finally:
        loop.close()


def test_ac4_cli_sandbox_and_api_endpoint_e2e(tmp_path, workspace, monkeypatch, capsys):
    _sandbox()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")

    exp_code = """
import json
print(json.dumps({
    "verdict": "refuted",
    "finding": "Linear scan was faster than building an index for N=5 items.",
    "metrics": {"n": 5}
}))
"""
    llm_reply = json.dumps(
        {
            "question": "Is indexing faster for N=5?",
            "hypothesis": "Building an index beats linear scan at N=5.",
            "hub": "perf",
            "prior_atom_id": None,
            "code": exp_code,
        }
    )
    fake_rt = FakeRuntime([llm_reply])
    monkeypatch.setattr(cli, "_runtime", lambda: fake_rt)

    rc = cli.main(["sandbox", "--replay", "2026-09-29"])
    assert rc == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["status"] == "ok"
    assert cli_out["date"] == "2026-09-29"
    assert cli_out["verdict"] == "refuted"

    with TestClient(app) as client:
        resp = client.get("/api/mind/sandbox", params={"limit": 30})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        assert isinstance(body["items"], list)
        latest = body["items"][0]
        assert latest["title"] == "life: dream sandbox"
        assert "refuted" in json.dumps(latest)


# ===========================================================================
# AC-5: AST Hardening & Anti-Toy-Experiment No-Op Guard (Slice 2)
# ===========================================================================


@pytest.mark.parametrize(
    "mod",
    [
        "importlib",
        "importlib.util",
        "importlib.machinery",
        "builtins",
    ],
)
def test_ac5_validate_code_rejects_importlib_and_builtins_imports(mod):
    sb = _sandbox()
    top = mod.split(".")[0]
    assert top in sb.FORBIDDEN_IMPORTS

    ok1, reason1 = sb.validate_code(f"import {mod}\nx = 1\n")
    assert ok1 is False
    assert reason1 is not None and top in reason1

    ok2, reason2 = sb.validate_code(f"from {mod} import something\n")
    assert ok2 is False
    assert reason2 is not None and top in reason2


@pytest.mark.parametrize(
    "builtin_name,call_snippet,ref_snippet",
    [
        ("getattr", "getattr(obj, 'attr')", "fn = getattr\n"),
        ("setattr", "setattr(obj, 'attr', 1)", "fn = setattr\n"),
        ("delattr", "delattr(obj, 'attr')", "fn = delattr\n"),
        ("globals", "globals()", "g = globals\n"),
        ("locals", "locals()", "l = locals\n"),
        ("vars", "vars(obj)", "v = vars\n"),
        ("open", "open('/etc/passwd')", "o = open\n"),
    ],
)
def test_ac5_validate_code_rejects_reflection_and_open_builtins(
    builtin_name, call_snippet, ref_snippet
):
    sb = _sandbox()
    assert builtin_name in sb.FORBIDDEN_BUILTINS

    ok_call, reason_call = sb.validate_code(call_snippet)
    assert ok_call is False
    assert isinstance(reason_call, str) and builtin_name in reason_call

    ok_ref, reason_ref = sb.validate_code(ref_snippet)
    assert ok_ref is False
    assert isinstance(reason_ref, str) and builtin_name in reason_ref


def test_ac5_build_prompt_instructs_skip_when_no_testable_hypothesis():
    sb = _sandbox()
    messages = sb._build_prompt({}, "", DATE)
    combined = "\n".join(m.get("content", "") for m in messages)
    assert '"skip": true' in combined or "'skip': true" in combined
    assert "no-testable-hypothesis" in combined


def test_ac5_sandbox_run_noop_guard_skips_execution_and_lesson(conn, workspace, monkeypatch):
    sb = _sandbox()
    exec_calls = []

    def fail_if_executed(*args, **kwargs):
        exec_calls.append((args, kwargs))
        raise AssertionError("execute_experiment must NOT be called when skip=true")

    monkeypatch.setattr(sb, "execute_experiment", fail_if_executed)

    llm_reply = json.dumps({"skip": True, "reason": "no-testable-hypothesis"})
    rt = FakeRuntime([llm_reply])
    out = sb.run(conn, rt, now=NOW)

    assert exec_calls == []
    assert out["status"] == "skipped"
    assert out["reason"] == "no-testable-hypothesis"
    assert out["date"] == DATE
    assert out["atom_id"] is None

    # No lesson atom created
    lesson_count = conn.execute("SELECT COUNT(*) FROM m_atoms WHERE type='lesson'").fetchone()[0]
    assert lesson_count == 0

    # Event emitted with status="skipped" and reason="no-testable-hypothesis"
    ev = conn.execute(
        "SELECT title, detail FROM events WHERE title='life: dream sandbox'"
    ).fetchone()
    assert ev is not None
    detail = json.loads(ev["detail"])
    assert detail["status"] == "skipped"
    assert detail["reason"] == "no-testable-hypothesis"


def test_ac5_sandbox_run_status_skip_variant_defaults_reason(conn, workspace, monkeypatch):
    sb = _sandbox()
    exec_calls = []

    def spy_exec(*args, **kwargs):
        exec_calls.append((args, kwargs))
        return {
            "ok": False,
            "status": "rejected",
            "error": "empty code",
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "result": None,
        }

    monkeypatch.setattr(sb, "execute_experiment", spy_exec)

    llm_reply = json.dumps({"status": "skip"})
    rt = FakeRuntime([llm_reply])
    out = sb.run(conn, rt, now=NOW)

    assert exec_calls == []
    assert out == {
        "status": "skipped",
        "reason": "no-testable-hypothesis",
        "date": DATE,
        "atom_id": None,
    }


# ===========================================================================
# AC-6: Live 7-Day ROI Scorecard (sandbox.roi_report, CLI --roi, API /roi)
# ===========================================================================


def _seed_sandbox_roi_fixture(conn):
    """Seed sandbox runs, sandbox lesson atoms, served exposures, and feedback signals."""
    sb = _sandbox()
    anchor_dt = datetime(2026, 9, 30, 14, 0)
    anchor_ms = int(anchor_dt.timestamp() * 1000)
    day_ms = 86_400_000

    # Outside window (10 days before anchor): 1 run event + 1 old sandbox atom
    old_ms = anchor_ms - 10 * day_ms
    sb._emit_event(
        conn,
        when_ms=old_ms,
        summary="old sandbox run outside window",
        severity="info",
        detail={"date": "2026-09-20", "status": "ok", "verdict": "confirmed"},
    )
    old_atom = mind.remember(
        conn,
        "Old finding outside 7d window.",
        atype="lesson",
        hubs=["agent"],
        source="memory/dreaming/sandbox/2026-09-20.md",
        session_id="sandbox",
    )
    conn.execute("UPDATE m_atoms SET created=? WHERE id=?", (old_ms, old_atom["id"]))
    conn.commit()
    rank.record_served(conn, [old_atom["id"]], now=old_ms + 1000)

    # Inside 7-day window: 5 sandbox events (1 confirmed, 1 refuted, 1 inconclusive, 1 skipped, 1 failed)
    sb._emit_event(
        conn,
        when_ms=anchor_ms - 5 * day_ms,
        summary="confirmed run",
        severity="info",
        detail={"date": "2026-09-25", "status": "ok", "verdict": "confirmed"},
    )
    sb._emit_event(
        conn,
        when_ms=anchor_ms - 4 * day_ms,
        summary="refuted run",
        severity="info",
        detail={"date": "2026-09-26", "status": "ok", "verdict": "refuted"},
    )
    sb._emit_event(
        conn,
        when_ms=anchor_ms - 3 * day_ms,
        summary="inconclusive run",
        severity="info",
        detail={"date": "2026-09-27", "status": "ok", "verdict": "inconclusive"},
    )
    sb._emit_event(
        conn,
        when_ms=anchor_ms - 2 * day_ms,
        summary="skipped run",
        severity="info",
        detail={
            "date": "2026-09-28",
            "status": "skipped",
            "reason": "no-testable-hypothesis",
        },
    )
    sb._emit_event(
        conn,
        when_ms=anchor_ms - 1 * day_ms,
        summary="failed run",
        severity="warning",
        detail={"date": "2026-09-29", "status": "failed", "error": "nonzero exit"},
    )

    # 2 sandbox lesson atoms inside window + 1 non-sandbox atom inside window
    a1 = mind.remember(
        conn,
        "Confirmed lesson: WAL mode readers do not block writers.",
        atype="lesson",
        hubs=["sqlite"],
        source="memory/dreaming/sandbox/2026-09-25.md",
        session_id="sandbox",
    )
    a2 = mind.remember(
        conn,
        "Refuted lesson: linear scan beats index for N=5.",
        atype="lesson",
        hubs=["perf"],
        source="memory/dreaming/sandbox/2026-09-26.md",
        session_id="sandbox",
    )
    non_sb = mind.remember(
        conn,
        "Regular CLI observation not from sandbox.",
        atype="lesson",
        hubs=["general"],
        source="cli-note",
        session_id="cli",
    )

    # Pin created timestamps inside [anchor_ms - 7*day_ms, anchor_ms]
    conn.execute("UPDATE m_atoms SET created=? WHERE id=?", (anchor_ms - 5 * day_ms, a1["id"]))
    conn.execute("UPDATE m_atoms SET created=? WHERE id=?", (anchor_ms - 4 * day_ms, a2["id"]))
    conn.execute("UPDATE m_atoms SET created=? WHERE id=?", (anchor_ms - 3 * day_ms, non_sb["id"]))
    conn.commit()

    # Record served exposures via rank.record_served (a1: 3 times, a2: 1 time, non_sb: 5 times)
    rank.record_served(conn, [a1["id"]], now=anchor_ms - 4 * day_ms)
    rank.record_served(conn, [a1["id"]], now=anchor_ms - 3 * day_ms)
    rank.record_served(conn, [a1["id"], a2["id"]], now=anchor_ms - 2 * day_ms)
    for i in range(5):
        rank.record_served(conn, [non_sb["id"]], now=anchor_ms - (i + 1) * 1000)

    # Record feedback signals via mind.record_feedback / mind.feedback
    record_fb = getattr(mind, "record_feedback", mind.feedback)
    record_fb(conn, a1["id"], "used", source="agent", session_id="ses_1")
    record_fb(conn, a1["id"], "used", source="agent", session_id="ses_2")
    record_fb(conn, a1["id"], "useful", source="agent", session_id="ses_2")
    record_fb(conn, a2["id"], "noise", source="agent", session_id="ses_3")
    record_fb(conn, a2["id"], "unused", source="agent", session_id="ses_3")
    record_fb(conn, non_sb["id"], "useful", source="agent", session_id="ses_4")

    return {
        "anchor_ms": anchor_ms,
        "a1_id": a1["id"],
        "a2_id": a2["id"],
        "non_sb_id": non_sb["id"],
    }


def test_ac6_roi_report_computes_windowed_metrics_and_clamps_days(conn):
    sb = _sandbox()
    seeded = _seed_sandbox_roi_fixture(conn)
    report = sb.roi_report(conn, days=7, at_ms=seeded["anchor_ms"])

    assert report["days"] == 7
    assert report["runs"] == {
        "total": 5,
        "ok": 3,
        "skipped": 1,
        "failed": 1,
        "confirmed": 1,
        "refuted": 1,
        "inconclusive": 1,
    }
    assert report["lessons_created"] == 2
    assert report["served"] == 4
    assert report["used"] == 2
    assert report["useful"] == 1
    assert report["noise"] == 1
    assert report["unused"] == 1

    assert isinstance(report["lessons"], list)
    assert len(report["lessons"]) == 2
    by_id = {item["id"]: item for item in report["lessons"]}
    assert set(by_id.keys()) == {seeded["a1_id"], seeded["a2_id"]}

    required_lesson_keys = {
        "id",
        "label",
        "text",
        "hub",
        "created",
        "source",
        "served",
        "used",
        "useful",
        "noise",
        "unused",
    }
    for item in report["lessons"]:
        assert required_lesson_keys <= set(item.keys())

    l1 = by_id[seeded["a1_id"]]
    assert l1["hub"] == "sqlite"
    assert l1["source"] == "memory/dreaming/sandbox/2026-09-25.md"
    assert l1["served"] == 3
    assert l1["used"] == 2
    assert l1["useful"] == 1
    assert l1["noise"] == 0
    assert l1["unused"] == 0

    l2 = by_id[seeded["a2_id"]]
    assert l2["hub"] == "perf"
    assert l2["source"] == "memory/dreaming/sandbox/2026-09-26.md"
    assert l2["served"] == 1
    assert l2["used"] == 0
    assert l2["useful"] == 0
    assert l2["noise"] == 1
    assert l2["unused"] == 1

    # Boundary: days <= 0 clamps to 1
    clamped = sb.roi_report(conn, days=0, at_ms=seeded["anchor_ms"])
    assert clamped["days"] == 1
    clamped_neg = sb.roi_report(conn, days=-5, at_ms=seeded["anchor_ms"])
    assert clamped_neg["days"] == 1


def _seed_live_roi_db(db_file: Path) -> str:
    sb = _sandbox()
    c = db.connect(db_file)
    db.init_db(c)
    try:
        now_ms = int(datetime.now().timestamp() * 1000)
        sb._emit_event(
            c,
            when_ms=now_ms - 10_000,
            summary="live confirmed run",
            severity="info",
            detail={"date": DATE, "status": "ok", "verdict": "confirmed"},
        )
        atom = mind.remember(
            c,
            "Empirical ROI test lesson from sandbox.",
            atype="lesson",
            hubs=["sqlite"],
            source=f"memory/dreaming/sandbox/{DATE}.md",
            session_id="sandbox",
        )
        rank.record_served(c, [atom["id"]], now=now_ms - 5_000)
        rank.record_served(c, [atom["id"]], now=now_ms - 2_000)
        record_fb = getattr(mind, "record_feedback", mind.feedback)
        record_fb(c, atom["id"], "used", source="agent", session_id="ses_cli")
        record_fb(c, atom["id"], "useful", source="agent", session_id="ses_cli")
        record_fb(c, atom["id"], "noise", source="agent", session_id="ses_cli")
        return str(atom["id"])
    finally:
        c.close()


def test_ac6_cli_sandbox_roi(tmp_path, monkeypatch, capsys):
    db_file = tmp_path / "neobrain.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    atom_id = _seed_live_roi_db(db_file)

    rc = cli.main(["sandbox", "--roi", "--days", "7"])
    assert rc == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["days"] == 7
    assert cli_report["runs"] == {
        "total": 1,
        "ok": 1,
        "skipped": 0,
        "failed": 0,
        "confirmed": 1,
        "refuted": 0,
        "inconclusive": 0,
    }
    assert cli_report["lessons_created"] == 1
    assert cli_report["served"] == 2
    assert cli_report["used"] == 1
    assert cli_report["useful"] == 1
    assert cli_report["noise"] == 1
    assert cli_report["unused"] == 0
    assert len(cli_report["lessons"]) == 1
    assert cli_report["lessons"][0]["id"] == atom_id


def test_ac6_api_sandbox_roi_endpoint(tmp_path, monkeypatch):
    db_file = tmp_path / "neobrain.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    atom_id = _seed_live_roi_db(db_file)

    with TestClient(app) as client:
        resp = client.get("/api/mind/sandbox/roi", params={"days": 7})
        assert resp.status_code == 200
        api_report = resp.json()
        assert api_report["days"] == 7
        assert api_report["runs"] == {
            "total": 1,
            "ok": 1,
            "skipped": 0,
            "failed": 0,
            "confirmed": 1,
            "refuted": 0,
            "inconclusive": 0,
        }
        assert api_report["lessons_created"] == 1
        assert api_report["served"] == 2
        assert api_report["used"] == 1
        assert api_report["useful"] == 1
        assert api_report["noise"] == 1
        assert api_report["unused"] == 0
        assert len(api_report["lessons"]) == 1
        assert api_report["lessons"][0]["id"] == atom_id

