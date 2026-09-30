"""Tests for neobrain.dreams.reflect — the weekly soul ritual (port of reflect.sh)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from neobrain import config, dreams

SUNDAY = datetime(2026, 10, 4, 4, 0)   # weekday 6
WEDNESDAY = datetime(2026, 9, 30, 4, 0)  # weekday 2

IDENTITY_NEW = "# Identity\n\n- Name: TestBot\n- Creature: little daemon\n"
SOUL_NEW = "# Soul\n\n## Core Truths\n\n- Always be honest.\n"
REPLY = json.dumps(
    {
        "identity_md": IDENTITY_NEW,
        "soul_md": SOUL_NEW,
        "changes": [{"file": "IDENTITY.md", "summary": "Gave myself a name: TestBot."}],
    }
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
        return self.replies.pop(0) if self.replies else "{}"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "IDENTITY.md").write_text("# Identity\n\n- Name: (pick something)\n", encoding="utf-8")
    (ws / "SOUL.md").write_text(
        "# Soul\n\n## Core Truths\n\n- Be helpful.\n\n## Boundaries\n\n- Never run sudo.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(config.settings, "reflect_weekdays", "6")
    return ws


def test_reflect_writes_soul_files_and_records_preference(conn, workspace):
    rt = FakeRuntime([REPLY])
    result = dreams.reflect(conn, rt, now=SUNDAY)

    assert result["status"] == "ok"
    assert result["files"] == ["IDENTITY.md", "SOUL.md"]
    assert (workspace / "IDENTITY.md").read_text(encoding="utf-8") == IDENTITY_NEW
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == SOUL_NEW

    rows = list(conn.execute("SELECT text, source FROM m_atoms WHERE type='preference'"))
    assert any("TestBot" in (r["text"] or "") for r in rows)
    assert any(r["source"] == "IDENTITY.md" for r in rows)


def test_reflect_emits_an_event(conn, workspace):
    rt = FakeRuntime([REPLY])
    dreams.reflect(conn, rt, now=SUNDAY)

    row = conn.execute(
        "SELECT severity, detail FROM events WHERE title='life: soul-reflect' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["severity"] == "info"
    assert "IDENTITY.md" in row["detail"]


def test_reflect_skips_on_other_weekdays(conn, workspace):
    rt = FakeRuntime([REPLY])
    result = dreams.reflect(conn, rt, now=WEDNESDAY)

    assert result["status"] == "skipped"
    assert result["reason"] == "not-reflect-weekday"
    assert rt.calls == []


def test_reflect_is_once_per_day(conn, workspace):
    rt = FakeRuntime([REPLY])
    dreams.reflect(conn, rt, now=SUNDAY)
    second = dreams.reflect(conn, rt, now=SUNDAY)

    assert second["status"] == "skipped"
    assert len(rt.calls) == 1


def test_reflect_weekday_is_configurable(conn, workspace, monkeypatch):
    monkeypatch.setattr(config.settings, "reflect_weekdays", "2")  # Wednesday
    rt = FakeRuntime([REPLY])
    result = dreams.reflect(conn, rt, now=WEDNESDAY)

    assert result["status"] == "ok"
    assert len(rt.calls) == 1


def test_reflect_survives_llm_down(conn, workspace):
    rt = FakeRuntime(error=RuntimeError("gateway down"))
    result = dreams.reflect(conn, rt, now=SUNDAY)

    assert result["status"] == "error"
    row = conn.execute("SELECT severity, detail FROM events WHERE title='life: soul-reflect'").fetchone()
    assert row["severity"] == "warning"
    assert "gateway down" in row["detail"]


def test_reflect_never_raises_on_unexpected_error(conn, workspace, monkeypatch):
    def boom(_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(dreams, "dreams_path", boom)
    rt = FakeRuntime([REPLY])
    result = dreams.reflect(conn, rt, now=SUNDAY)

    assert result["status"] == "error"
    assert "boom" in result["error"]


def test_reflect_prompt_is_verbatim(conn, workspace):
    rt = FakeRuntime([REPLY])
    dreams.reflect(conn, rt, now=SUNDAY)

    prompt = rt.calls[0]["messages"][0]["content"]
    assert "You are running your weekly SELF-REFLECTION ritual (OpenClaw style) for 2026-10-04." in prompt
    assert "Core Truths" in prompt
    assert "Constraints: do not use sudo; edit only IDENTITY.md, SOUL.md" in prompt
    assert "$DATE" not in prompt
