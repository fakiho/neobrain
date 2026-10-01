"""neoBrain CLI — drive the mind without any UI.

Usage:
  neobrain init [--install-plugin]     # create the store + install the plugin
  neobrain start                       # run the daemon (API + dashboard + life loop)
  neobrain ui                          # print/open the dashboard URL
  neobrain remember "…" --type lesson --hubs a,b
  neobrain recall "…" [--json] [--lexical]
  neobrain wakeup
  neobrain link <src> <target> <kind>
  neobrain forget <atom_id>
  neobrain consolidate
  neobrain feedback <atom_id> used|useful|noise
  neobrain embed [--force] [--limit N]
  neobrain dreams [--phase light|rem|deep|all] [--replay YYYY-MM-DD]
  neobrain reflect
  neobrain ingest [--source …]

PORT-NOTE: the old timeline ops subcommands (stats/events/day/search/why/
sessions) are NOT ported — the Observatory dashboard covers exploration, and
`serve` became `start` (bind + life loop from config). Only the mind commands
and the new daemon/distribution commands live here; no business logic.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sqlite3
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db, embeddings, mind

# Repo root = parents[2] (src/neobrain/cli.py); adapters/ lives there.
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "adapters" / "opencode" / "neoBrain-memory"
PLUGIN_NAME = "neobrain-memory"
# PORT-NOTE: the opencode plugin this replaces; a leftover symlink must be
# removed by the user (never deleted silently by init).
LEGACY_PLUGIN_NAME = "timeline-memory"


# --- helpers -------------------------------------------------------------


def _session(args: argparse.Namespace) -> str:
    # PORT-NOTE: the old TIMELINE_SESSION env name is now NEOBRAIN_SESSION.
    return args.session or os.environ.get("NEOBRAIN_SESSION") or "cli"


def _retry(fn, *args, tries: int = 8, **kwargs):
    """Retry a write on SQLite 'database is locked' (the life loop is a second
    writer; WAL allows one at a time)."""
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == tries - 1:
                raise
            time.sleep(0.4 * (i + 1))


def _runtime():
    """The native LLM runtime module (SPEC §7) used by the dream/reflect passes."""
    from . import runtime

    return runtime


def _parse_bind(value: str) -> tuple[str, int]:
    """Split ``host:port`` (``NEOBRAIN_BIND``). Falls back to sane defaults."""
    text = (value or "").strip()
    if not text:
        return "0.0.0.0", 9192
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        return text, 9192
    if host.startswith("[") and host.endswith("]"):  # [::1]:9192 -> ::1
        host = host[1:-1]
    return host or "0.0.0.0", int(port)


# --- lifecycle / distribution -------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    print(f"initialised {config.DB_PATH}")
    print("next steps:")
    print("  1. neobrain start                 # daemon: API + dashboard + life loop")
    print(f"  2. open {config.settings.api}   # the Observatory")
    print('  3. neobrain remember "…"          # store your first memory')
    if args.install_plugin:
        return _install_plugin()
    return 0


def _install_plugin() -> int:
    plugins_dir = Path.home() / ".opencode" / "plugins"
    legacy = plugins_dir / LEGACY_PLUGIN_NAME
    target = plugins_dir / PLUGIN_NAME

    if legacy.exists() or legacy.is_symlink():
        print(
            f"refusing to install the plugin: {legacy} is still present.\n"
            "The neoBrain plugin replaces the timeline plugin — two memory "
            "plugins would both inject and double-rate memories. Remove the "
            f"old symlink first (e.g. `rm {legacy}`), then re-run "
            "`neobrain init --install-plugin`.",
            file=sys.stderr,
        )
        return 1

    if not PLUGIN_SRC.is_dir():
        print(f"plugin source not found: {PLUGIN_SRC}", file=sys.stderr)
        return 1

    if target.is_symlink():
        if target.resolve() == PLUGIN_SRC.resolve():
            print(f"plugin already installed: {target} -> {PLUGIN_SRC}")
            return 0
        print(
            f"refusing to overwrite existing symlink {target} -> {target.resolve()}",
            file=sys.stderr,
        )
        return 1
    if target.exists():
        print(f"refusing to overwrite existing {target}", file=sys.stderr)
        return 1

    plugins_dir.mkdir(parents=True, exist_ok=True)
    target.symlink_to(PLUGIN_SRC, target_is_directory=True)
    print(f"installed plugin: {target} -> {PLUGIN_SRC}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    import uvicorn

    from . import api

    host, port = _parse_bind(config.settings.bind)
    # PORT-NOTE: the life loop is NOT started here — it lives in the app's
    # lifespan (api.py) so `NEOBRAIN_LIFE_ENABLED=0` and uvicorn-without-CLI
    # both behave identically.
    print(f"neoBrain daemon on http://{host}:{port}  (dashboard {config.settings.api})")
    uvicorn.run(api.app, host=host, port=port)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    url = config.settings.api
    print(url)
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - headless hosts have no browser
        print(f"(could not open a browser: {exc})", file=sys.stderr)
    return 0


def cmd_dreams(args: argparse.Namespace) -> int:
    from . import dreams

    conn = db.connect()
    db.init_db(conn)
    try:
        replay = getattr(args, "replay", None)
        if replay:
            # Operational fallback for a night that failed (e.g. empty LLM
            # answers): re-run light→rem→deep for that date. The fake clock at
            # 22:30 makes _dream_date resolve to DATE itself and the
            # once-per-night marker (checked against this clock) cannot match a
            # past day, so the night re-runs; DREAMS.md's own guard and
            # remember --dedupe keep a replay idempotent.
            y, m, d = (int(p) for p in replay.split("-"))
            res = dreams.run(conn, _runtime(), now=datetime(y, m, d, 22, 30))
        else:
            phase = args.phase
            if phase in (None, "all"):
                res = dreams.run(conn, _runtime())
            elif "phase" in inspect.signature(dreams.run).parameters:
                res = dreams.run(conn, _runtime(), phase=phase)
            else:
                # The module exposes only the full pass; be explicit instead of
                # pretending a single phase ran.
                print(
                    f"note: neobrain.dreams.run has no per-phase selector; running "
                    f"the full pass instead of '{phase}'",
                    file=sys.stderr,
                )
                res = dreams.run(conn, _runtime())
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    from . import dreams

    conn = db.connect()
    db.init_db(conn)
    try:
        res = dreams.reflect(conn, _runtime())
    finally:
        conn.close()
    print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import runner

    conn = db.connect()
    db.init_db(conn)
    sources = tuple(args.source) if args.source else ("opencode", "git", "docs")
    try:
        summary = runner.run(conn, sources)
    finally:
        conn.close()
    print(json.dumps(summary, indent=2, default=str))
    return 0


# --- mind tools ----------------------------------------------------------


def cmd_remember(args: argparse.Namespace) -> int:
    conn = db.connect()
    _retry(db.init_db, conn)
    links: dict[str, list[str]] = {}
    for spec in args.link or []:
        if ":" in spec:
            kind, target = spec.split(":", 1)
            links.setdefault(kind, []).append(target)
    hubs = [h for h in (args.hubs.split(",") if args.hubs else []) if h]
    res = _retry(
        mind.remember, conn, args.text, atype=args.type, hubs=hubs or None,
        links=links or None, source=args.source, session_id=_session(args), label=args.label,
        dedupe=args.dedupe,
    )
    print(json.dumps(res, indent=2))
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    pack = mind.recall(conn, args.query, limit=args.limit, session_id=_session(args),
                       semantic=not args.lexical)
    if args.json:
        print(json.dumps(pack, indent=2))
        return 0
    print(f"# {pack['count']} memories for {args.query!r}\n")
    for a in pack["atoms"]:
        print(f"[{a['type']}] {a['label']}")
        if a["text"]:
            print(f"    {a['text'][:180]}")
    if pack["links"]:
        print("\nrelations:")
        for l in pack["links"][:30]:
            print(f"  {l['source']}  --{l['type']}-->  {l['target']}")
    return 0


def cmd_wakeup(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    print(json.dumps(mind.wake_up(conn, _session(args)), indent=2))
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    conn = db.connect()
    _retry(db.init_db, conn)
    print(json.dumps(_retry(mind.link, conn, args.source, args.target, args.kind, _session(args)), indent=2))
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    conn = db.connect()
    _retry(db.init_db, conn)
    print(json.dumps(_retry(mind.forget, conn, args.atom_id, _session(args)), indent=2))
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    conn = db.connect()
    _retry(db.init_db, conn)
    print(json.dumps(_retry(mind.consolidate, conn, _session(args)), indent=2))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    conn = db.connect()
    _retry(db.init_db, conn)
    res = _retry(mind.feedback, conn, args.atom_id, args.signal,
                 source=args.source, session_id=_session(args))
    print(json.dumps(res, indent=2))
    return 0


def cmd_directives(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    print(json.dumps(mind.directives(conn, limit=args.limit), indent=2))
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    conn = db.connect()
    _retry(db.init_db, conn)
    res = _retry(mind.set_pin, conn, args.atom_id, not args.off, _session(args))
    print(json.dumps(res, indent=2))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_db(conn)
    summary = _retry(
        embeddings.backfill, conn, force=args.force,
        limit=args.limit if args.limit is not None else None,
    )
    print(json.dumps(summary, indent=2))
    return 0


# --- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="neobrain", description="neoBrain — the agent's own mind")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="create the store and print next steps")
    sp.add_argument("--install-plugin", action="store_true",
                    help="symlink the opencode plugin into ~/.opencode/plugins")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("start", help="run the daemon (API + dashboard + life loop)")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("ui", help="print and open the dashboard URL")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("dreams", help="run a nightly dream pass")
    sp.add_argument("--phase", choices=["light", "rem", "deep", "all"], default="all")
    sp.add_argument(
        "--replay",
        metavar="YYYY-MM-DD",
        help="re-run a past night's full dream pass (light→rem→deep); use for "
        "nights that failed (empty answers, gateway outage). Idempotent.",
    )
    sp.set_defaults(func=cmd_dreams)

    sp = sub.add_parser("reflect", help="run the weekly reflection pass")
    sp.set_defaults(func=cmd_reflect)

    sp = sub.add_parser("ingest", help="run ingestion adapters (one-shot)")
    sp.add_argument("--source", nargs="+", help="sources to run (default: all)")
    sp.set_defaults(func=cmd_ingest)

    # --- agent memory tools ---
    sp = sub.add_parser("remember", help="store a memory")
    sp.add_argument("text")
    sp.add_argument("--type", default="observation")
    sp.add_argument("--hubs", help="comma-separated hub ids")
    sp.add_argument("--link", action="append", help="kind:targetId (supersedes|derived-from|caused-by|consolidates|about)")
    sp.add_argument("--source")
    sp.add_argument("--label")
    sp.add_argument("--session")
    sp.add_argument("--dedupe", action="store_true",
                    help="return an existing identical atom (same text + source) instead of storing a duplicate")
    sp.set_defaults(func=cmd_remember)

    sp = sub.add_parser("recall", help="recall a relevant subgraph")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--lexical", action="store_true", help="force lexical-only ranking")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_recall)

    sp = sub.add_parser("embed", help="backfill local embeddings (idempotent)")
    sp.add_argument("--force", action="store_true", help="re-embed every atom")
    sp.add_argument("--limit", type=int, default=None, help="cap atoms embedded this run")
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("wakeup", help="compact wake-up pack (identity + open loops + recent)")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_wakeup)

    sp = sub.add_parser("link", help="wire two memories")
    sp.add_argument("source")
    sp.add_argument("target")
    sp.add_argument("kind")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_link)

    sp = sub.add_parser("forget", help="delete a memory")
    sp.add_argument("atom_id")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("consolidate", help="dream pass: merge hubs, promote patterns, resolve supersessions")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_consolidate)

    sp = sub.add_parser("feedback", help="record memory quality (used|useful|noise)")
    sp.add_argument("atom_id")
    sp.add_argument("signal", choices=list(mind.FEEDBACK_SIGNALS))
    sp.add_argument("--source")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("directives", help="show the standing directives re-injected every turn")
    sp.add_argument("--limit", type=int, default=8)
    sp.set_defaults(func=cmd_directives)

    sp = sub.add_parser("pin", help="pin/unpin a memory as a standing directive")
    sp.add_argument("atom_id")
    sp.add_argument("--off", action="store_true", help="unpin instead of pin")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_pin)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
