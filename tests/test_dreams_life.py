"""Life-loop integration: the rest window drives dreams/reflect exactly once.

Hermetic: probes and embeddings are patched out; dreams.run/reflect are stubbed
so no LLM or filesystem work happens. Scheduling is exercised against a mutable
clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from neobrain import config, embeddings, life

WEDNESDAY = datetime(2026, 9, 30, 2, 0)  # 02:00 = dream hour, quiet, weekday 2
SUNDAY = datetime(2026, 10, 4, 2, 0)


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kwargs) -> None:
        self.dt = self.dt + timedelta(**kwargs)


class FakeRuntime:
    def chat(self, messages, **kwargs) -> str:
        return "done"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_atom", lambda conn, atom_id: False)
    monkeypatch.setattr(life, "PROBES", (("llm", lambda conn=None: (True, "ok")),))


@pytest.fixture
def stub_dreams(monkeypatch):
    from neobrain import dreams

    calls = {"run": 0, "reflect": 0}

    def fake_run(conn, runtime, **kwargs):
        calls["run"] += 1
        return {"status": "ok", "date": "2026-09-29"}

    def fake_reflect(conn, runtime, **kwargs):
        calls["reflect"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(dreams, "run", fake_run)
    monkeypatch.setattr(dreams, "reflect", fake_reflect)
    return calls


def make_loop(tmp_path, dt):
    clock = Clock(dt)
    instance = life.LifeLoop(db_path=tmp_path / "neobrain.db", clock=clock, runtime=FakeRuntime())
    return instance, clock


def titles(conn):
    return [r[0] for r in conn.execute("SELECT title FROM events WHERE source='life'")]


def test_dream_hour_runs_dreams_once_per_night(tmp_path, stub_dreams, monkeypatch):
    monkeypatch.setattr(config.settings, "reflect_weekdays", "6")  # Sunday; now is Wed
    instance, clock = make_loop(tmp_path, WEDNESDAY)
    try:
        first = instance.tick()
        assert "dream" in first["ran"]
        assert "soul-reflect" not in first["ran"]
        assert stub_dreams["run"] == 1

        clock.advance(minutes=30)
        second = instance.tick()
        assert "dream" not in second["ran"]
        assert stub_dreams["run"] == 1
    finally:
        instance.close()


def test_reflect_not_run_on_other_weekdays(tmp_path, stub_dreams, monkeypatch):
    monkeypatch.setattr(config.settings, "reflect_weekdays", "6")
    instance, _clock = make_loop(tmp_path, WEDNESDAY)
    try:
        result = instance.tick()
        assert "soul-reflect" not in result["ran"]
        assert stub_dreams["reflect"] == 0
    finally:
        instance.close()


def test_reflect_runs_on_configured_weekday_once(tmp_path, stub_dreams, monkeypatch):
    monkeypatch.setattr(config.settings, "reflect_weekdays", "6")
    instance, clock = make_loop(tmp_path, SUNDAY)
    try:
        result = instance.tick()
        assert "dream" in result["ran"]
        assert "soul-reflect" in result["ran"]
        assert stub_dreams["reflect"] == 1

        clock.advance(minutes=30)
        instance.tick()
        assert stub_dreams["reflect"] == 1
    finally:
        instance.close()


def test_reflect_weekday_is_configurable(tmp_path, stub_dreams, monkeypatch):
    monkeypatch.setattr(config.settings, "reflect_weekdays", "2")  # Wednesday
    instance, _clock = make_loop(tmp_path, WEDNESDAY)
    try:
        result = instance.tick()
        assert "soul-reflect" in result["ran"]
        assert stub_dreams["reflect"] == 1
    finally:
        instance.close()


def test_life_emits_markers_when_runners_do_not(tmp_path, stub_dreams, monkeypatch):
    monkeypatch.setattr(config.settings, "reflect_weekdays", "6")
    instance, _clock = make_loop(tmp_path, SUNDAY)
    try:
        instance.tick()
        conn = instance.connect()
        assert "life: dream" in titles(conn)
        assert "life: soul-reflect" in titles(conn)
    finally:
        instance.close()
