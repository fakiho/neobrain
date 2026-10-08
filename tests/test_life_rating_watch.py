"""Tests for the rating-watch phase — the daily rating-volume snapshot.

It rides the life loop, so nothing here touches the network: the notifier is
patched, and embeddings/LLM are stubbed exactly as in ``test_life_loop``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from neobrain import config, embeddings, life, mind


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


class FakeRuntime:
    def __init__(self, replies=None):
        self.replies = list(replies or ["I keep working."])

    def chat(self, messages, **kwargs) -> str:
        return self.replies.pop(0) if self.replies else "done"


def ok_probe(conn=None):
    return True, "ok"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_atom", lambda conn, atom_id: False)
    monkeypatch.setattr(life, "PROBES", (("llm", ok_probe), ("db", ok_probe)))


@pytest.fixture
def watch(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "rating_watch_enabled", "1")
    monkeypatch.setattr(config.settings, "rating_watch_hour", 9)
    monkeypatch.setattr(config.settings, "rating_watch_days", 7)
    monkeypatch.setattr(config.settings, "rating_watch_until", "")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(life, "_notify", lambda title, text: sent.append((title, text)) or True)
    clock = Clock(datetime(2026, 10, 8, 9, 0, 0))  # 09:00 = the watch hour, not quiet
    instance = life.LifeLoop(db_path=tmp_path / "neobrain.db", clock=clock, runtime=FakeRuntime())
    yield instance, clock, sent
    instance.close()


def watch_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM events WHERE title='life: rating-watch' ORDER BY ts"
    ))


def test_disabled_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "rating_watch_enabled", "0")
    instance = life.LifeLoop(
        db_path=tmp_path / "n.db", clock=Clock(datetime(2026, 10, 8, 9, 0)), runtime=FakeRuntime()
    )
    try:
        instance.tick()
        assert watch_events(instance.connect()) == []
    finally:
        instance.close()


def test_snapshot_runs_once_and_flags_a_stall(watch):
    instance, _clock, sent = watch
    result = instance.tick()
    assert "rating-watch" in result["ran"]

    conn = instance.connect()
    events = watch_events(conn)
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["phase"] == "rating-watch"
    assert detail["snapshot"]["days"] == 7
    assert detail["snapshot"]["rated"] == 0
    assert events[0]["severity"] == "warning"  # zero ratings in the window
    assert len(sent) == 1                       # pinged once

    # A second tick the same hour/day must not record again.
    instance.tick()
    assert len(watch_events(conn)) == 1


def test_not_stalled_when_ratings_present(watch):
    instance, _clock, sent = watch
    conn = instance.connect()
    atom = mind.remember(conn, "a memory to rate", atype="lesson", hubs=["neobrain"])
    mind.feedback(conn, atom["id"], "useful", source="agent", session_id="ses_test")

    instance.tick()
    events = watch_events(conn)
    assert len(events) == 1
    assert events[0]["severity"] == "info"
    assert sent == []
    detail = json.loads(events[0]["detail"])
    assert detail["snapshot"]["useful"] == 1
    assert detail["snapshot"]["raters"] == 1


def test_stops_after_until(watch, monkeypatch):
    instance, _clock, sent = watch
    monkeypatch.setattr(config.settings, "rating_watch_until", "2026-10-07")
    instance.tick()
    assert watch_events(instance.connect()) == []
    assert sent == []


def test_rating_volume_counts_unranked(watch):
    instance, _clock, _sent = watch
    conn = instance.connect()
    mind.remember(conn, "never rated", atype="observation", hubs=["neobrain"])
    snap = mind.rating_volume(conn, days=7, at_ms=int(datetime(2026, 10, 8, 9, 0).timestamp() * 1000))
    assert snap["rated"] == 0
    assert snap["unranked"] >= 1
    assert snap["atoms"] >= 1
