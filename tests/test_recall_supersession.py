"""Recall hides atoms superseded by a newer active atom (read-time, append-only)."""
from __future__ import annotations

from neobrain import mind

T = 1_700_000_000_000
Q = "openwrt firewall nftables"


def _pair(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    conn.execute("INSERT INTO m_edges(source,target,type) VALUES('new','old','supersedes')")
    conn.commit()


def _ids(conn, **kw):
    return {a["id"] for a in mind.recall(conn, Q, semantic=False, **kw)["atoms"]}


def test_superseded_hidden_by_default(conn, add_atom):
    _pair(conn, add_atom)
    assert _ids(conn) == {"new"}
    # append-only: the old atom is still stored
    assert conn.execute("SELECT 1 FROM m_atoms WHERE id='old'").fetchone()


def test_include_superseded_returns_both(conn, add_atom):
    _pair(conn, add_atom)
    assert _ids(conn, include_superseded=True) == {"old", "new"}


def test_inactive_superseder_does_not_hide(conn, add_atom):
    _pair(conn, add_atom)
    for state in ("ignored", "archived"):
        conn.execute("INSERT OR REPLACE INTO m_rank(atom_id,quality,served,interacted,state,updated) "
                     "VALUES('new',0.5,0,0,?,?)", (state, T))
        conn.commit()
        assert _ids(conn) == {"old"}


def test_no_supersedes_edges_unaffected(conn, add_atom):
    add_atom("a", created=T, label="openwrt firewall", text="openwrt firewall nftables one")
    add_atom("b", created=T, label="openwrt nftables", text="openwrt firewall nftables two")
    assert _ids(conn) == {"a", "b"}
    assert _ids(conn, include_superseded=True) == {"a", "b"}
