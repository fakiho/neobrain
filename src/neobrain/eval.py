"""Deterministic memory-quality eval: no model, no network, no judge.

Two fixed-case checks, adapted from the general practice of auditing memory
quality with a golden set (no external project's data, prompts or code):

* **Retrieval quality** — a checked-in set of atoms and queries is loaded into
  a store with ``mind.remember`` and recalled lexically (``semantic=False``).
  Reported as recall@k and MRR.
* **Junk rate** — checked-in filler strings and short real memories are run
  through the write gate (``mind.remember_rejection``). Reported as how much
  filler is refused and how many real memories are wrongly refused.

``run(conn)`` needs an *empty* store (use a temp one); it never calls the
network and never touches a store you did not hand it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config, mind

GOLDEN_PATH = Path(__file__).resolve().parent / "eval_data" / "memory_golden.json"
# Recall deeper than k so MRR can score a hit that lands just outside the top k.
RANK_DEPTH = 10


def load_golden(path: Path | None = None) -> dict:
    with open(path or GOLDEN_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _retrieval(conn: sqlite3.Connection, golden: dict) -> dict:
    k = int(golden.get("k", 5))
    ids: dict[str, str] = {}  # golden key -> stored atom id
    for atom in golden["atoms"]:
        res = mind.remember(
            conn, atom["text"], atype=atom.get("type", "observation"),
            hubs=[atom["hub"]] if atom.get("hub") else None,
            source="eval:golden", session_id="eval",
        )
        ids[atom["key"]] = res["id"]
    keys = {v: k_ for k_, v in ids.items()}

    per_query: list[dict] = []
    failures: list[dict] = []
    for q in golden["queries"]:
        expected = list(q["expected"])
        pack = mind.recall(conn, q["query"], limit=RANK_DEPTH, session_id="eval", semantic=False)
        ranked = [keys.get(a["id"], a["id"]) for a in pack["atoms"]]
        hits = [key for key in expected if key in ranked[:k]]
        first = next((i + 1 for i, key in enumerate(ranked) if key in expected), None)
        row = {
            "id": q["id"], "query": q["query"], "expected": expected,
            "top": ranked[:k], "recall_at_k": len(hits) / len(expected),
            "reciprocal_rank": (1.0 / first) if first else 0.0, "first_hit_rank": first,
        }
        per_query.append(row)
        if len(hits) < len(expected):
            failures.append({"check": "retrieval", "id": q["id"], "query": q["query"],
                             "expected": expected, "top": ranked[:k]})
    n = len(per_query) or 1
    return {
        "k": k,
        "recall_at_k": round(sum(r["recall_at_k"] for r in per_query) / n, 4),
        "mrr": round(sum(r["reciprocal_rank"] for r in per_query) / n, 4),
        "queries": per_query,
        "failures": failures,
    }


def _junk(golden: dict) -> dict:
    junk = golden["junk"]
    filler_missed = [t for t in junk["filler"] if mind.remember_rejection(t) is None]
    real_wrong = [t for t in junk["real"] if mind.remember_rejection(t) is not None]
    return {
        "filler_rejected": len(junk["filler"]) - len(filler_missed),
        "filler_total": len(junk["filler"]),
        "real_rejected": len(real_wrong),
        "real_total": len(junk["real"]),
        "filler_accepted": filler_missed,
        "real_wrongly_rejected": real_wrong,
    }


def run(conn: sqlite3.Connection, golden: dict | None = None) -> dict:
    """Run both checks against an empty store and return the report.

    Keys: ``recall_at_k``, ``mrr``, ``k``, ``queries`` (per-query detail),
    ``failures`` (retrieval misses and junk-gate misses), ``junk``.
    """
    mind._ensure_schema(conn)
    if conn.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0]:
        raise ValueError("eval needs an empty store; pass a fresh temporary database")
    golden = golden or load_golden()
    # remember() embeds best-effort; keep the eval offline even if embeddings
    # are configured, then restore the caller's setting.
    embed_was = config.EMBED_ENABLED
    config.EMBED_ENABLED = False
    try:
        report = _retrieval(conn, golden)
    finally:
        config.EMBED_ENABLED = embed_was
    report["junk"] = _junk(golden)
    for text in report["junk"]["filler_accepted"]:
        report["failures"].append({"check": "junk", "kind": "filler_accepted", "text": text})
    for text in report["junk"]["real_wrongly_rejected"]:
        report["failures"].append({"check": "junk", "kind": "real_rejected", "text": text})
    return report
