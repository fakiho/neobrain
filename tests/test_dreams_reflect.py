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


DIARY = """<!-- openclaw:dreaming:diary:start -->
---
*October 1, 2026 at 2:00 AM*
first night body
---
*October 2, 2026 at 2:00 AM*
second night body
<!-- openclaw:dreaming:diary:end -->
"""


def _write_evidence(ws, rem_days=("2026-10-01", "2026-10-02")):
    (ws / "DREAMS.md").write_text(DIARY, encoding="utf-8")
    rem = ws / "memory" / "dreaming" / "rem"
    rem.mkdir(parents=True, exist_ok=True)
    for day in rem_days:
        (rem / f"{day}.md").write_text(f"rem body for {day}", encoding="utf-8")


def test_reflect_inlines_dreams_and_rem(conn, workspace):
    _write_evidence(workspace)
    rt = FakeRuntime([REPLY])
    dreams.reflect(conn, rt, now=SUNDAY)

    prompt = rt.calls[0]["messages"][0]["content"]
    assert "recent character: DREAMS.md + memory/dreaming/rem" in prompt
    assert "*October 2, 2026 at 2:00 AM*" in prompt
    assert "second night body" in prompt
    assert "### rem 2026-10-02" in prompt
    assert "rem body for 2026-10-02" in prompt


def test_reflect_compaction_keeps_every_id(conn, workspace, monkeypatch):
    _write_evidence(workspace)
    monkeypatch.setattr(config.settings, "reflect_context_chars", 120)
    rt = FakeRuntime([REPLY])
    dreams.reflect(conn, rt, now=SUNDAY)

    prompt = rt.calls[0]["messages"][0]["content"]
    # Every id survives the squeeze, even though the bodies do not.
    assert "*October 1, 2026 at 2:00 AM*" in prompt
    assert "*October 2, 2026 at 2:00 AM*" in prompt
    assert "### rem 2026-10-01" in prompt
    assert "### rem 2026-10-02" in prompt
    assert "id kept]" in prompt
    # Persona files are never compacted: SOUL.md's red line is intact.
    assert "Never run sudo." in prompt


def test_reflect_compaction_never_touches_persona(conn, workspace, monkeypatch):
    _write_evidence(workspace)
    monkeypatch.setattr(config.settings, "reflect_context_chars", 0)
    rt = FakeRuntime([REPLY])
    dreams.reflect(conn, rt, now=SUNDAY)

    prompt = rt.calls[0]["messages"][0]["content"]
    assert "### IDENTITY.md" in prompt
    assert "### SOUL.md" in prompt
    assert "Never run sudo." in prompt


class TierRuntime:
    """Fails the strong tier, answers on cheap; records the tier order tried."""

    def __init__(self, cheap_reply=REPLY, empty=False):
        self.cheap_reply = cheap_reply
        self.empty = empty
        self.models: list[str] = []

    def chat(self, messages, **kwargs) -> str:
        model = kwargs.get("model")
        self.models.append(model)
        if model == "strong":
            raise RuntimeError("LLM gateway returned empty content (finish_reason=length)")
        return "" if self.empty else self.cheap_reply


def test_reflect_falls_back_to_cheap_tier(conn, workspace):
    rt = TierRuntime()
    result = dreams.reflect(conn, rt, now=SUNDAY)

    assert rt.models == ["strong", "cheap"]
    assert result["status"] == "ok"
    assert result["model"] == "cheap"
    assert (workspace / "IDENTITY.md").read_text(encoding="utf-8") == IDENTITY_NEW


def test_reflect_empty_answer_is_a_failure_not_no_changes(conn, workspace):
    rt = TierRuntime(empty=True)
    result = dreams.reflect(conn, rt, now=SUNDAY)

    assert result["status"] == "error"
    assert "empty answer" in result["error"]
    row = conn.execute("SELECT severity FROM events WHERE title='life: soul-reflect'").fetchone()
    assert row["severity"] == "warning"
