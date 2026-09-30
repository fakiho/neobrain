"""CLI tests: call ``neobrain.cli.main`` directly against a temp store.

Hermetic: the store lives in ``tmp_path``, embeddings are disabled by
``conftest.py``, and ``HOME`` is redirected for the plugin-install test.
"""
from __future__ import annotations

import json

import pytest

from neobrain import cli, config

PLUGIN_SRC = cli.PLUGIN_SRC


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    return tmp_path


def test_remember_then_recall_roundtrip(store, capsys):
    assert cli.main(["remember", "The daemon binds 127.0.0.1:9477", "--hubs", "neobrain",
                     "--source", "test", "--label", "bind"]) == 0
    atom = json.loads(capsys.readouterr().out)
    assert atom["id"].startswith("m_")
    assert atom["label"] == "bind"
    assert atom["hubs"] == ["neobrain"]

    assert cli.main(["recall", "daemon binds", "--json"]) == 0
    pack = json.loads(capsys.readouterr().out)
    assert pack["count"] >= 1
    assert any(a["id"] == atom["id"] for a in pack["atoms"])

    # human-readable path (no --json)
    assert cli.main(["recall", "daemon binds"]) == 0
    assert "bind" in capsys.readouterr().out


def test_remember_dedupe_is_idempotent(store, capsys):
    cli.main(["remember", "same memory", "--source", "test", "--dedupe"])
    first = json.loads(capsys.readouterr().out)
    cli.main(["remember", "same memory", "--source", "test", "--dedupe"])
    second = json.loads(capsys.readouterr().out)
    assert first["id"] == second["id"]
    assert second.get("deduped") is True


def test_feedback_records_and_validates_signal(store, capsys):
    cli.main(["remember", "feedback target", "--source", "test"])
    atom = json.loads(capsys.readouterr().out)
    assert cli.main(["feedback", atom["id"], "useful"]) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True

    with pytest.raises(SystemExit):
        cli.main(["feedback", atom["id"], "bogus"])


def test_wakeup_returns_pack(store, capsys):
    cli.main(["remember", "a preference to remember", "--type", "preference"])
    capsys.readouterr()
    assert cli.main(["wakeup"]) == 0
    pack = json.loads(capsys.readouterr().out)
    assert set(pack) == {"identity", "open_loops", "recent", "hubs"}


def test_init_creates_store_and_prints_next_steps(store, capsys):
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "initialised" in out
    assert "neobrain start" in out
    assert (store / "neobrain.db").exists()


def test_init_install_plugin_symlinks_into_fake_home(store, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert cli.main(["init", "--install-plugin"]) == 0
    target = home / ".opencode" / "plugins" / "neobrain-memory"
    assert target.is_symlink()
    assert target.resolve() == PLUGIN_SRC.resolve()
    assert "installed plugin" in capsys.readouterr().out

    # Idempotent: a second run reports it is already installed.
    assert cli.main(["init", "--install-plugin"]) == 0
    assert "already installed" in capsys.readouterr().out


def test_init_refuses_when_legacy_timeline_plugin_present(store, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    legacy = home / ".opencode" / "plugins" / "timeline-memory"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    assert cli.main(["init", "--install-plugin"]) == 1
    err = capsys.readouterr().err
    assert "timeline-memory" in err
    assert not (home / ".opencode" / "plugins" / "neobrain-memory").exists()


def test_parse_bind_handles_host_port_and_fallbacks():
    assert cli._parse_bind("127.0.0.1:9477") == ("127.0.0.1", 9477)
    assert cli._parse_bind("0.0.0.0:9192") == ("0.0.0.0", 9192)
    assert cli._parse_bind("") == ("0.0.0.0", 9192)
    assert cli._parse_bind("localhost") == ("localhost", 9192)
    assert cli._parse_bind("[::1]:9192") == ("::1", 9192)
