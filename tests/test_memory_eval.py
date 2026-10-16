"""Deterministic memory-quality eval: golden retrieval set and junk gate.

Hermetic: a temp store, lexical recall only, no network, no model.
"""
from __future__ import annotations

import json

import pytest

from neobrain import cli, config, db, eval as memory_eval, mind

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "remember_quality", "1")
    monkeypatch.setattr(config.settings, "remember_min_chars", 3)
    c = db.connect(tmp_path / "neobrain.db")
    db.init_db(c)
    yield c
    c.close()


def test_retrieval_quality_meets_floor(store):
    report = memory_eval.run(store)
    assert report["k"] == 5
    assert report["recall_at_k"] >= 0.8, report["failures"]
    assert report["mrr"] >= 0.6, report["failures"]
    assert len(report["queries"]) >= 8


def test_run_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    reports = []
    for name in ("a.db", "b.db"):
        c = db.connect(tmp_path / name)
        db.init_db(c)
        r = memory_eval.run(c)
        c.close()
        reports.append((r["recall_at_k"], r["mrr"], [q["top"] for q in r["queries"]]))
    assert reports[0] == reports[1]


def test_junk_gate(store):
    junk = memory_eval.run(store)["junk"]
    assert junk["real_rejected"] == 0, junk["real_wrongly_rejected"]
    assert junk["real_total"] >= 10 and junk["filler_total"] >= 10
    # every golden filler sample is refused
    assert junk["filler_accepted"] == []
    assert junk["filler_rejected"] == junk["filler_total"]


def test_run_refuses_a_non_empty_store(store):
    mind.remember(store, "Use tabs in Python files", atype="preference")
    with pytest.raises(ValueError):
        memory_eval.run(store)


def test_cli_eval_prints_json_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "live.db")
    assert cli.main(["eval"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {"recall_at_k", "mrr", "k", "queries", "failures", "junk"} <= set(report)
    assert report["recall_at_k"] >= 0.8
    # the eval ran in a throwaway store, never the configured one
    assert not (tmp_path / "live.db").exists()
