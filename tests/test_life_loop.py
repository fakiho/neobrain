"""Tests for neobrain.life — the phase scheduler, no network, tmp-path DBs.

The probes are patched out (they are the network/boundary of perceive) and
embeddings.embed_atom is a no-op, so nothing here touches Ollama or the LLM
gateway. Scheduling semantics are exercised against a mutable clock.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from neobrain import config, embeddings, life, mind


# --- test doubles -----------------------------------------------------------


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kwargs) -> None:
        self.dt = self.dt + timedelta(**kwargs)


class FakeRuntime:
    """Scripted runtime.chat — records calls, returns canned thoughts."""

    def __init__(self, replies=None):
        self.replies = list(replies or ["I notice the quiet and keep working."])
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, **kwargs})
        return self.replies.pop(0) if self.replies else "done"


def ok_probe(conn=None):
    return True, "ok"


def fail_probe(conn=None):
    return False, "boom"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Keep every test offline and deterministic."""
    monkeypatch.setattr(embeddings, "embed_atom", lambda conn, atom_id: False)
    monkeypatch.setattr(life, "PROBES", (("llm", ok_probe), ("db", ok_probe)))


@pytest.fixture
def loop(tmp_path):
    clock = Clock(datetime(2026, 9, 30, 12, 0, 0))  # midday: not quiet
    llm = FakeRuntime()
    instance = life.LifeLoop(db_path=tmp_path / "neobrain.db", clock=clock, runtime=llm)
    yield instance, clock, llm
    instance.close()


def life_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM events WHERE source='life' ORDER BY ts ASC, rowid ASC"
    ))


def titles(conn: sqlite3.Connection) -> list[str]:
    return [r["title"] for r in life_events(conn)]


def atoms_from(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM m_atoms WHERE source=?", (source,)))


# --- phase ordering ---------------------------------------------------------


def test_one_tick_runs_perceive_reflect_act_in_order(loop):
    instance, _clock, llm = loop
    result = instance.tick()
    assert result["ran"] == ["perceive", "reflect", "act"]
    assert result["quiet"] is False
    assert titles(instance.connect()) == [
        "life: perceive", "life: reflect", "life: act",
    ]
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "strong"


def test_events_use_the_life_conventions(loop):
    instance, clock, _llm = loop
    instance.tick()
    row = instance.connect().execute(
        "SELECT * FROM events WHERE title='life: perceive'"
    ).fetchone()
    assert row["source"] == "life"
    assert row["lane"] == "notice"
    assert row["category"] == "life"
    assert row["actor"] == "agent:neobrain"
    assert row["severity"] == "info"
    assert row["ingested_at"] > 0
    detail = json.loads(row["detail"])
    assert detail["phase"] == "perceive"
    assert detail["next_due"] > row["ts"]
    from neobrain.models import event_id
    assert row["id"] == event_id("life", f"perceive:{row['ts']}")


def test_act_stores_its_thought_as_an_observation_atom(loop):
    instance, _clock, llm = loop
    expected = llm.replies[0]
    instance.tick()
    atoms = atoms_from(instance.connect(), "life:act")
    assert len(atoms) == 1
    assert atoms[0]["type"] == "observation"
    assert atoms[0]["text"] == expected


# --- quiet hours ------------------------------------------------------------


@pytest.mark.parametrize("hour", [23, 0, 3, 7])
def test_quiet_hours_run_only_perceive(loop, hour):
    instance, clock, llm = loop
    clock.dt = clock.dt.replace(hour=hour)
    result = instance.tick()
    assert result["quiet"] is True
    assert result["ran"] == ["perceive"]
    conn = instance.connect()
    assert "life: act" not in titles(conn)
    assert "life: reflect" not in titles(conn)
    assert llm.calls == []


def test_non_quiet_hour_8_is_not_quiet(loop):
    instance, clock, _llm = loop
    clock.dt = clock.dt.replace(hour=8)
    assert instance.is_quiet() is False


# --- interval gating --------------------------------------------------------


def test_intervals_gate_reruns(loop):
    instance, clock, _llm = loop
    assert instance.tick()["ran"] == ["perceive", "reflect", "act"]

    clock.advance(minutes=1)
    assert instance.tick()["ran"] == []  # nothing due yet

    clock.advance(minutes=15)  # 16 min since perceive; reflect=30, act=240
    assert instance.tick()["ran"] == ["perceive"]

    clock.advance(minutes=15)  # 31 min: perceive + reflect due
    assert instance.tick()["ran"] == ["perceive", "reflect"]

    clock.advance(minutes=300)  # everything due
    assert instance.tick()["ran"] == ["perceive", "reflect", "act"]


def test_state_is_derived_so_a_restart_loses_nothing(loop):
    instance, clock, _llm = loop
    assert instance.tick()["ran"] == ["perceive", "reflect", "act"]

    # A fresh loop object over the same DB (simulating a daemon restart) must
    # see the phases as recently run and stay idle.
    restarted = life.LifeLoop(db_path=instance.db_path, clock=clock, runtime=FakeRuntime())
    try:
        assert restarted.tick()["ran"] == []
    finally:
        restarted.close()


# --- perceive failures ------------------------------------------------------


def test_perceive_failure_becomes_an_atom_once_per_streak(loop, monkeypatch):
    instance, clock, _llm = loop
    monkeypatch.setattr(life, "PROBES", (("llm", fail_probe), ("db", ok_probe)))

    result = instance.tick()
    conn = instance.connect()
    assert "life: act" in titles(conn)
    atoms = atoms_from(conn, "life:perceive")
    assert len(atoms) == 1
    assert "llm" in atoms[0]["text"]

    # Same streak, next due perceive: throttled, no second atom.
    clock.advance(minutes=16)
    instance.tick()
    assert len(atoms_from(conn, "life:perceive")) == 1

    # Recovery clears the streak, so a later failure is remembered again.
    monkeypatch.setattr(life, "PROBES", (("llm", ok_probe), ("db", ok_probe)))
    clock.advance(minutes=16)
    instance.tick()
    monkeypatch.setattr(life, "PROBES", (("llm", fail_probe), ("db", ok_probe)))
    clock.advance(minutes=16)
    instance.tick()
    assert len(atoms_from(conn, "life:perceive")) == 2


def test_perceive_failure_event_has_warning_severity(loop, monkeypatch):
    instance, _clock, _llm = loop
    monkeypatch.setattr(life, "PROBES", (("llm", fail_probe), ("db", ok_probe)))
    instance.tick()
    row = instance.connect().execute(
        "SELECT severity, detail FROM events WHERE title='life: perceive'"
    ).fetchone()
    assert row["severity"] == "warning"
    detail = json.loads(row["detail"])
    assert detail["failures"] == ["llm"]
    assert "llm" in detail["checks"]


# --- rest / dream hook ------------------------------------------------------


def test_dream_hook_emits_not_wired_once_per_night(loop):
    instance, clock, _llm = loop
    clock.dt = clock.dt.replace(hour=2)  # dream hour, and quiet
    first = instance.tick()
    assert first["ran"] == ["perceive", "dream"]

    clock.advance(minutes=30)
    second = instance.tick()
    assert "dream" not in second["ran"]

    conn = instance.connect()
    dreams = [r for r in life_events(conn) if r["title"] == "life: dream"]
    assert len(dreams) == 1
    assert json.loads(dreams[0]["detail"])["summary"] == "dreams not wired yet (S6)"


def test_dream_hook_does_not_fire_outside_dream_hour(loop):
    instance, clock, _llm = loop
    clock.dt = clock.dt.replace(hour=3)  # quiet, but not life_dream_hour (2)
    assert instance.tick()["ran"] == ["perceive"]


# --- run_forever ------------------------------------------------------------


def test_run_forever_respects_max_ticks(loop, monkeypatch):
    instance, _clock, _llm = loop
    monkeypatch.setattr(life.time, "sleep", lambda seconds: None)
    assert instance.run_forever(max_ticks=2) == {"ticks": 2}


# --- act failure path -------------------------------------------------------


def test_act_failure_is_recorded_but_does_not_crash(loop):
    instance, _clock, _llm = loop

    class BrokenRuntime:
        def chat(self, messages, **kwargs):
            raise RuntimeError("gateway down")

    instance._runtime = BrokenRuntime()
    result = instance.tick()
    assert result["ran"] == ["perceive", "reflect", "act"]
    row = instance.connect().execute(
        "SELECT severity, detail FROM events WHERE title='life: act'"
    ).fetchone()
    assert row["severity"] == "warning"
    assert "gateway down" in json.loads(row["detail"])["summary"]
    assert atoms_from(instance.connect(), "life:act") == []
