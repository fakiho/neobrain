"""Deterministic rank + forgetting engine (SPEC §4.2/§4.3).

Two independent axes, both stored per atom in ``m_rank``:

* **quality** — the rank: the product of three bounded factors, so *each* of
  them can push a memory under the forgetting threshold::

      quality = clamp01(feedback · decay · conn)
      decay   = decay_floor + (1 - decay_floor)·recency
      conn    = conn_floor  + (1 - conn_floor)·connectivity

  with ``feedback`` the smoothed mean of the recorded signals
  (``useful=1.0``, ``used=0.6``, ``noise=0.0``) against a ``0.5`` prior over
  ``rank_prior_n`` pseudo-counts, ``recency = 0.5 ** (age_days /
  rank_half_life_days)`` from the latest of creation / last feedback / last
  serve, and ``connectivity`` the normalised incident edge degree. The floors
  temper age and graph isolation; they do not decide a memory's fate on their
  own, because feedback is the one factor that can reach zero. An additive
  blend cannot express this — recency/connectivity then act as irreducible
  floors, so no amount of ``noise`` can forget a fresh memory (the S4 review).

* **exposure** — ``served + 3·interacted``; how often the memory has actually
  been surfaced and touched. Exposure never *raises* quality, it only decides
  *how* a low-quality atom is forgotten.

Forgetting is a per-atom state machine (``run_eviction`` classifies, it never
deletes):

===========================  ==================================================
``active``                   default; eligible for recall
``ignored``                  quality < ``rank_ignore_below`` and exposure <
                             ``rank_archive_exposure``: low quality that was
                             never even tried — excluded from recall, row kept,
                             zero maintenance cost, never deleted
``archived``                 quality < ``rank_ignore_below`` and exposure ≥
                             ``rank_archive_exposure``: served repeatedly and
                             never rated useful — excluded from recall but
                             recoverable (data intact)
===========================  ==================================================

Promotion is the only way back: a new ``useful``/``used`` signal on an ignored
or archived atom proves value and flips it to ``active``.

Everything here is pure and deterministic — same DB state (and same ``now``)
yields the same scores. There are **no LLM calls in this module, ever**: the
model has zero discretion over retention. Weights and thresholds come only from
``config.settings``.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from . import config
from .models import now_ms

# --- locked constants (SPEC §4.2 / m_2d90e7f99e54) ----------------------

#: Feedback signal -> quality in [0, 1].
SIGNAL_VALUE: dict[str, float] = {"useful": 1.0, "used": 0.6, "noise": 0.0}
#: Neutral value for an unexpected signal (should not occur; keeps the mean well defined).
_DEFAULT_SIGNAL = 0.5
#: Signals that count as the agent having interacted with a served memory.
_INTERACTED_SIGNALS = frozenset(SIGNAL_VALUE)
#: Signals that prove value and may promote an ignored/archived atom.
_PROMOTING_SIGNALS = frozenset(("useful", "used"))

#: Valid states of the forgetting machine.
STATES = ("active", "ignored", "archived")

Counts = tuple[int, int, int]                    # (used, useful, noise)
FeedbackStat = tuple[int, int, int, int | None]  # counts + latest feedback ts
_MS_PER_DAY = 86_400_000.0


# --- tunables (config only) ---------------------------------------------

@dataclass(frozen=True)
class Params:
    """Immutable snapshot of the rank knobs from ``config.settings``."""

    decay_floor: float
    conn_floor: float
    half_life_days: float
    prior_n: float
    ignore_below: float
    archive_exposure: int


def params() -> Params:
    """Read the rank floors/thresholds from config (never hard-coded)."""
    s = config.settings
    return Params(
        decay_floor=s.rank_decay_floor,
        conn_floor=s.rank_conn_floor,
        half_life_days=s.rank_half_life_days,
        prior_n=s.rank_prior_n,
        ignore_below=s.rank_ignore_below,
        archive_exposure=s.rank_archive_exposure,
    )


# --- pure scoring functions ---------------------------------------------

def clamp01(value: float) -> float:
    """Clamp to the closed unit interval."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def feedback_score(counts: Counts, prior_n: float) -> float:
    """Smoothed mean signal value with a 0.5 prior over ``prior_n`` counts.

    ``(Σ value·count + 0.5·prior_n) / (n + prior_n)`` — an atom with no signals
    sits at exactly 0.5; many signals pull it toward the observed mean.
    """
    used, useful, noise = counts
    n = used + useful + noise
    total = (
        used * SIGNAL_VALUE["used"]
        + useful * SIGNAL_VALUE["useful"]
        + noise * SIGNAL_VALUE["noise"]
        + 0.5 * prior_n
    )
    denom = n + prior_n
    return total / denom if denom > 0 else 0.5


def recency_score(age_days: float, half_life_days: float) -> float:
    """``0.5 ** (age_days / half_life_days)`` — 1.0 at age 0, 0.5 at one life."""
    if half_life_days <= 0 or age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def connectivity_score(degree: int, max_degree: int) -> float:
    """``ln(1+degree) / ln(1+max_degree)`` in [0, 1] (0 when the batch is flat)."""
    if max_degree <= 0 or degree <= 0:
        return 0.0
    if degree >= max_degree:
        return 1.0
    return math.log(1.0 + degree) / math.log(1.0 + max_degree)


def quality_score(
    feedback: float,
    recency: float,
    connectivity: float,
    *,
    decay_floor: float,
    conn_floor: float,
) -> float:
    """The clamped product of the three bounded factors (the rank).

    ``feedback`` is already in [0, 1] and is the only factor that can reach
    zero, so model feedback alone can forget a memory (SPEC §4.2/4.3).
    ``recency`` and ``connectivity`` are floored: they temper quality but never
    decide a memory's fate on their own.
    """
    decay = decay_floor + (1.0 - decay_floor) * recency
    conn = conn_floor + (1.0 - conn_floor) * connectivity
    return clamp01(feedback * decay * conn)


def exposure_score(served: int, interacted: int) -> int:
    """``served + 3·interacted`` — how much the atom has been surfaced/touched."""
    return int(served) + 3 * int(interacted)


def classify(
    quality: float,
    exposure: int,
    *,
    ignore_below: float,
    archive_exposure: int,
) -> str:
    """Map (quality, exposure) to the state a low-quality atom falls into."""
    if quality < ignore_below:
        return "archived" if exposure >= archive_exposure else "ignored"
    return "active"


def _score_atom(
    *,
    created: int | None,
    last_served: int | None,
    feedback: FeedbackStat,
    degree: int,
    max_degree: int,
    now: int,
    p: Params,
) -> float:
    """Quality of one atom given its feedback stat and graph degree."""
    counts: Counts = (feedback[0], feedback[1], feedback[2])
    latest = created or 0
    if last_served and last_served > latest:
        latest = last_served
    if feedback[3] and feedback[3] > latest:
        latest = feedback[3]
    age_days = max(0.0, (now - latest) / _MS_PER_DAY)
    return quality_score(
        feedback_score(counts, p.prior_n),
        recency_score(age_days, p.half_life_days),
        connectivity_score(degree, max_degree),
        decay_floor=p.decay_floor,
        conn_floor=p.conn_floor,
    )


# --- store reads --------------------------------------------------------

def _feedback_stats(conn: sqlite3.Connection) -> dict[str, FeedbackStat]:
    """atom_id -> (used, useful, noise, latest feedback ts)."""
    acc: dict[str, list] = {}
    for aid, signal, n, last in conn.execute(
        "SELECT atom_id, signal, COUNT(*), MAX(ts) FROM m_feedback GROUP BY atom_id, signal"
    ):
        if not aid:
            continue
        rec = acc.setdefault(aid, [0, 0, 0, None])
        if signal == "used":
            rec[0] = n
        elif signal == "useful":
            rec[1] = n
        elif signal == "noise":
            rec[2] = n
        if last is not None and (rec[3] is None or last > rec[3]):
            rec[3] = last
    return {aid: (r[0], r[1], r[2], r[3]) for aid, r in acc.items()}


def _degrees(conn: sqlite3.Connection) -> tuple[dict[str, int], int]:
    """Incident edge degree per atom (hub ``about`` edges included) + the max."""
    deg: dict[str, int] = {}
    for aid, n in conn.execute(
        """WITH incident AS (
               SELECT source AS a FROM m_edges
               UNION ALL
               SELECT target AS a FROM m_edges)
           SELECT a, COUNT(*) FROM incident GROUP BY a"""
    ):
        deg[aid] = n
    return deg, (max(deg.values()) if deg else 0)


def _last_served(conn: sqlite3.Connection) -> dict[str, int | None]:
    return {
        r[0]: r[1]
        for r in conn.execute("SELECT atom_id, last_served FROM m_rank")
    }


# --- store writes -------------------------------------------------------

def ensure_row(conn: sqlite3.Connection, atom_id: str, *, now: int | None = None) -> None:
    """Create a default (active, quality 0.5) rank row if one is missing."""
    ts = now if now is not None else now_ms()
    conn.execute(
        """INSERT OR IGNORE INTO m_rank
             (atom_id, quality, served, interacted, state, updated)
           VALUES (?, 0.5, 0, 0, 'active', ?)""",
        (atom_id, ts),
    )


def recompute(conn: sqlite3.Connection, *, now: int | None = None) -> dict:
    """Recompute ``quality`` for every atom from the current DB state.

    Batch scorer: the connectivity term is normalised against the batch's max
    degree, so it must see the whole graph at once. Preserves counters, state
    and ``last_served``. Deterministic for a fixed ``now``.
    """
    ts = now if now is not None else now_ms()
    p = params()
    stats = _feedback_stats(conn)
    degrees, max_degree = _degrees(conn)
    last_served = _last_served(conn)
    scored = 0
    for aid, created in conn.execute("SELECT id, created FROM m_atoms"):
        q = _score_atom(
            created=created,
            last_served=last_served.get(aid),
            feedback=stats.get(aid, (0, 0, 0, None)),
            degree=degrees.get(aid, 0),
            max_degree=max_degree,
            now=ts,
            p=p,
        )
        conn.execute(
            """INSERT INTO m_rank
                 (atom_id, quality, served, interacted, last_served, state, state_since, updated)
               VALUES (?, ?, 0, 0, NULL, 'active', NULL, ?)
               ON CONFLICT(atom_id) DO UPDATE SET quality = excluded.quality, updated = excluded.updated""",
            (aid, q, ts),
        )
        scored += 1
    conn.commit()
    return {"scored": scored, "max_degree": max_degree, "now": ts}


def recompute_atom(conn: sqlite3.Connection, atom_id: str, *, now: int | None = None) -> float | None:
    """Recompute one atom's quality (used by the feedback path)."""
    row = conn.execute("SELECT id, created FROM m_atoms WHERE id = ?", (atom_id,)).fetchone()
    if not row:
        return None
    ts = now if now is not None else now_ms()
    p = params()
    degrees, max_degree = _degrees(conn)
    q = _score_atom(
        created=row[1],
        last_served=_last_served(conn).get(atom_id),
        feedback=_feedback_stats(conn).get(atom_id, (0, 0, 0, None)),
        degree=degrees.get(atom_id, 0),
        max_degree=max_degree,
        now=ts,
        p=p,
    )
    ensure_row(conn, atom_id, now=ts)
    conn.execute("UPDATE m_rank SET quality = ?, updated = ? WHERE atom_id = ?", (q, ts, atom_id))
    return q


def on_feedback(conn: sqlite3.Connection, atom_id: str, signal: str) -> float | None:
    """Apply one recorded feedback signal to the rank store.

    Counts the interaction, promotes an ignored/archived atom when the signal
    proves value, and refreshes the atom's quality. Returns the new quality
    (None when the atom no longer exists). Never raises on a locked store — the
    caller treats rank bookkeeping as best-effort.
    """
    ts = now_ms()
    ensure_row(conn, atom_id, now=ts)
    if signal in _INTERACTED_SIGNALS:
        conn.execute("UPDATE m_rank SET interacted = interacted + 1 WHERE atom_id = ?", (atom_id,))
    if signal in _PROMOTING_SIGNALS:
        conn.execute(
            "UPDATE m_rank SET state = 'active', state_since = ? WHERE atom_id = ? AND state <> 'active'",
            (ts, atom_id),
        )
    quality = recompute_atom(conn, atom_id, now=ts)
    conn.commit()
    return quality


def record_served(conn: sqlite3.Connection, atom_ids: list[str], *, now: int | None = None) -> int:
    """Count one exposure for each atom just served by recall."""
    ts = now if now is not None else now_ms()
    served = 0
    for aid in atom_ids:
        conn.execute(
            """INSERT INTO m_rank
                 (atom_id, quality, served, interacted, last_served, state, state_since, updated)
               VALUES (?, 0.5, 1, 0, ?, 'active', NULL, ?)
               ON CONFLICT(atom_id) DO UPDATE SET
                 served = served + 1,
                 last_served = excluded.last_served,
                 updated = excluded.updated""",
            (aid, ts, ts),
        )
        served += 1
    conn.commit()
    return served


def run_eviction(conn: sqlite3.Connection, *, now: int | None = None) -> dict:
    """Classify low-quality active atoms as ignored or archived. Never deletes.

    Only *demotes*: an atom that is already ignored/archived stays so until a
    new ``useful``/``used`` signal promotes it (see ``on_feedback``), which is
    what makes "ignored" durable rather than a function of the current score.
    """
    ts = now if now is not None else now_ms()
    p = params()
    # Every atom carries a rank row; a missing one is a fresh, unevicted atom.
    conn.execute(
        """INSERT OR IGNORE INTO m_rank
             (atom_id, quality, served, interacted, state, updated)
           SELECT id, 0.5, 0, 0, 'active', ? FROM m_atoms""",
        (ts,),
    )
    ignored = archived = 0
    for aid, quality, served, interacted in conn.execute(
        "SELECT atom_id, quality, served, interacted FROM m_rank WHERE state = 'active'"
    ):
        state = classify(
            quality,
            exposure_score(served, interacted),
            ignore_below=p.ignore_below,
            archive_exposure=p.archive_exposure,
        )
        if state == "active":
            continue
        conn.execute(
            "UPDATE m_rank SET state = ?, state_since = ?, updated = ? WHERE atom_id = ?",
            (state, ts, ts, aid),
        )
        if state == "archived":
            archived += 1
        else:
            ignored += 1
    conn.commit()
    return {"ignored": ignored, "archived": archived, "now": ts}


# --- reporting ----------------------------------------------------------

def states(conn: sqlite3.Connection) -> dict[str, int]:
    """Count of rank rows per state (missing rows treated as ``active``)."""
    counts = {s: 0 for s in STATES}
    for state, n in conn.execute("SELECT state, COUNT(*) FROM m_rank GROUP BY state"):
        if state in counts:
            counts[state] = n
    ranked = conn.execute("SELECT COUNT(*) FROM m_rank").fetchone()[0]
    unranked = conn.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0] - ranked
    counts["active"] += max(0, unranked)
    return counts
