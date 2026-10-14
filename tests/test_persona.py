"""Persona placeholders and the Identity-tab editor's write path."""
from __future__ import annotations

import pytest

from neobrain import persona


def test_seed_writes_only_what_is_missing(tmp_path):
    written = persona.seed(tmp_path)
    assert written == ["IDENTITY.md", "SOUL.md"]
    identity = (tmp_path / "IDENTITY.md").read_text(encoding="utf-8")
    assert "- Name: (pick a name)" in identity  # a placeholder the parser skips
    assert "## Boundaries" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")

    # a hand-written persona must survive a re-init
    (tmp_path / "IDENTITY.md").write_text("# Identity\n\n- Name: Kept\n", encoding="utf-8")
    assert persona.seed(tmp_path) == []
    assert "Name: Kept" in (tmp_path / "IDENTITY.md").read_text(encoding="utf-8")


def test_seed_is_a_noop_without_a_workspace(monkeypatch):
    monkeypatch.setattr(persona, "persona_dir", lambda: None)
    assert persona.seed() == []


def test_save_refuses_anything_but_the_two_persona_files(tmp_path, monkeypatch):
    monkeypatch.setattr(persona, "persona_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        persona.save("USER.md", "nope")
    with pytest.raises(ValueError):
        persona.save("../secrets.env", "nope")
    assert list(tmp_path.iterdir()) == []


def test_save_rejects_a_soul_that_drops_a_section(tmp_path, monkeypatch):
    monkeypatch.setattr(persona, "persona_dir", lambda: tmp_path)
    (tmp_path / "SOUL.md").write_text("# Soul\n\n## Core Truths\n\n- one\n\n## Boundaries\n\n- two\n", encoding="utf-8")
    with pytest.raises(ValueError):
        persona.save("SOUL.md", "# Soul\n\n## Core Truths\n\n- rewritten\n")
    assert "## Boundaries" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")  # untouched


def test_save_writes_and_keeps_a_trailing_newline(tmp_path, monkeypatch):
    monkeypatch.setattr(persona, "persona_dir", lambda: tmp_path)
    path = persona.save("IDENTITY.md", "# Identity\n\n- Name: Dale")
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Name: Dale" in path.read_text(encoding="utf-8")


def test_init_seeds_the_persona(tmp_path, monkeypatch, capsys):
    from neobrain import cli, config

    # init re-resolves config from the environment, so the workspace has to
    # come through the env var rather than a patched module global.
    ws = tmp_path / "ws"
    monkeypatch.setenv("NEOBRAIN_WORKSPACE", str(ws))
    monkeypatch.setenv("NEOBRAIN_DATA_DIR", str(tmp_path / "data"))
    # Settings reads env at construction; rebuild it so the test's env wins,
    # then let init's own apply_db_overrides recompute the derived paths.
    config.settings = config.Settings()
    assert cli.main(["init"]) == 0
    assert (ws / "IDENTITY.md").exists() and (ws / "SOUL.md").exists()
    assert "persona placeholder" in capsys.readouterr().out
