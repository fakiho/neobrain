"""Tests for neobrain.dreams.run — hermetic: tmp workspace, FakeRuntime.

No network, no real Ollama/LLM. Embeddings are disabled in conftest.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from neobrain import config, dreams

NOW = datetime(2026, 9, 30, 2, 0)  # 02:00 -> dream date is the previous day
DATE = "2026-09-29"

LIGHT = "# Light Sleep\n\n- fixed the DNS resolver\n- shipped the ingest port\n"
REM = (
    "The server hummed a low B flat as the resolver found its way home.\n\n"
    "A small poem: packets, / arriving after the rain / each one says hello."
)
DEEP_JSON = json.dumps(
    {
        "operations": [
            {
                "candidateKey": "dns-resolver-fix",
                "action": "added",
                "text": "The DNS resolver was fixed on 2026-09-29.",
                "hub": "dns",
            }
        ]
    }
)
DEEP_PROSE = (
    "# Deep Sleep\n\nTonight the mind tidied a small corner.\n\n"
    "- The DNS resolver was fixed on 2026-09-29.\n\n"
    "Added: dns-resolver-fix. Merged: nothing. Superseded: nothing.\n"
)


class FakeRuntime:
    def __init__(self, replies=None, error: Exception | None = None):
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, **kwargs})
        if self.error:
            raise self.error
        return self.replies.pop(0) if self.replies else "done"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / f"{DATE}.md").write_text(
        "The DNS resolver was fixed today.\n\n## Decision: ship ingest\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKSPACE", ws)
    return ws


def phases(root):
    return root / "light" / f"{DATE}.md", root / "rem" / f"{DATE}.md", root / "deep" / f"{DATE}.md"


def test_run_writes_phase_files_and_dreams_entry(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    result = dreams.run(conn, rt, now=NOW)

    assert result["status"] == "ok"
    root = workspace / "memory" / "dreaming"
    light, rem, deep = phases(root)
    assert light.read_text(encoding="utf-8").startswith("# Light Sleep")
    assert REM.splitlines()[0] in rem.read_text(encoding="utf-8")
    assert "# Deep Sleep" in deep.read_text(encoding="utf-8")
    assert (root / "deep" / f"{DATE}.json").exists()

    dreams_md = (workspace / "DREAMS.md").read_text(encoding="utf-8")
    assert "*September 29, 2026 at " in dreams_md
    assert "the resolver found its way home" in dreams_md


def test_run_uses_cheap_then_strong_and_json_for_deep(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    dreams.run(conn, rt, now=NOW)

    assert [c["model"] for c in rt.calls] == ["cheap", "strong", "strong"]
    assert rt.calls[2]["json_mode"] is True
    assert rt.calls[0]["json_mode"] is False


def test_run_emits_phase_and_marker_events(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    dreams.run(conn, rt, now=NOW)

    titles = [r[0] for r in conn.execute(
        "SELECT title FROM events WHERE source='life' ORDER BY ts, rowid"
    )]
    for title in ("life: dream light", "life: dream rem", "life: dream deep", "life: dream"):
        assert title in titles


def test_run_stores_durable_lessons_via_mind_remember(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    result = dreams.run(conn, rt, now=NOW)

    assert result["phases"]["deep"]["lessons"]
    rows = list(conn.execute("SELECT text, source, hub FROM m_atoms WHERE type='lesson'"))
    assert any("DNS resolver was fixed" in (r["text"] or "") for r in rows)
    assert any((r["source"] or "").endswith(f"deep/{DATE}.md") for r in rows)


def test_run_is_idempotent_within_the_night(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    dreams.run(conn, rt, now=NOW)

    second = dreams.run(conn, rt, now=NOW)
    assert second["status"] == "skipped"
    assert len(rt.calls) == 3  # no extra LLM calls

    dreams_md = (workspace / "DREAMS.md").read_text(encoding="utf-8")
    assert dreams_md.count("*September 29, 2026 at ") == 1


def test_run_survives_llm_down(conn, workspace):
    rt = FakeRuntime(error=RuntimeError("gateway down"))
    result = dreams.run(conn, rt, now=NOW)

    assert result["status"] == "partial"
    assert len(result["errors"]) == 3
    row = conn.execute("SELECT severity, detail FROM events WHERE title='life: dream'").fetchone()
    assert row["severity"] == "error"
    assert "gateway down" in row["detail"]


def test_run_falls_back_to_bullets_when_deep_returns_prose(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_PROSE])
    result = dreams.run(conn, rt, now=NOW)

    assert result["status"] == "ok"
    deep = (workspace / "memory" / "dreaming" / "deep" / f"{DATE}.md").read_text(encoding="utf-8")
    assert "# Deep Sleep" in deep
    lessons = [r[0] for r in conn.execute("SELECT text FROM m_atoms WHERE type='lesson'")]
    assert any("DNS resolver was fixed" in (t or "") for t in lessons)


def test_run_never_raises_on_unexpected_error(conn, workspace, monkeypatch):
    def boom(_conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(dreams, "_wake_pack", boom)
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    result = dreams.run(conn, rt, now=NOW)

    assert result["status"] == "error"
    assert "boom" in result["error"]


def test_prompts_are_verbatim_and_fully_substituted(conn, workspace):
    rt = FakeRuntime([LIGHT, REM, DEEP_JSON])
    dreams.run(conn, rt, now=NOW)

    light_prompt = rt.calls[0]["messages"][0]["content"]
    assert "You are the agent running the LIGHT phase of the nightly DREAM routine for 2026-09-29." in light_prompt
    assert "shipped ingest" not in light_prompt  # light only sees the day note
    assert "$" not in light_prompt.replace("—", "")  # no unsubstituted $VAR tokens

    rem_prompt = rt.calls[1]["messages"][0]["content"]
    assert "You are keeping a dream diary. Write a single entry in first person." in rem_prompt
    assert "# Light Sleep" in rem_prompt  # prior light file injected

    deep_prompt = rt.calls[2]["messages"][0]["content"]
    assert "This is a consolidation pass: decide how each durable fact joins the Timeline" in deep_prompt
    assert "neoBrain runner contract" in deep_prompt
    assert "--- light phase (" in deep_prompt  # earlier phases injected
