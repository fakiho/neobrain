"""Vector embeddings for the Timeline mind — provider-configurable.

Two interchangeable providers (``NEOBRAIN_EMBED_PROVIDER``):

* ``ollama`` (default) — ``embeddinggemma`` served on this host. Memory text
  never leaves the machine. 768-dim, 2048-token context; CPU inference is
  slower than a cloud embedder (see ``NEOBRAIN_EMBED_TIMEOUT``).
* ``litellm`` — Google ``gemini-embedding-2`` through the local LiteLLM
  gateway. **Privacy:** atom text is sent off-host to Google (user-approved
  2026-09-27; see ``INFRA_TIMELINE.md`` §4a). Kept as an opt-in alternative.

Storage is a single side table ``m_embeddings`` keyed by ``atom_id``. Vectors are
L2-normalised float32 blobs, so cosine similarity is a plain dot product and no
heavy numeric dependency (numpy) is required. The ``model`` column carries the
*provider-qualified* key (``ollama:embeddinggemma``), so switching provider
re-embeds everything without a migration.

Everything here is *best-effort*: if the provider is down or misconfigured the
helpers return ``None`` and callers fall back to lexical-only recall. Nothing
here may ever raise into an ingest run or a ``remember`` call.
"""
from __future__ import annotations

import json
import math
import sqlite3
import urllib.error
import urllib.request
from array import array

from . import config
from .models import content_hash, now_ms
from .schema import SCHEMA

# PORT-NOTE: SCHEMA moved to neobrain/schema.py (single authoritative module;
# applying the full merged schema is idempotent, so ensure_schema() is
# behaviorally unchanged).

# Batch size for one provider round-trip. The corpus is small (hundreds of
# atoms), so 16 keeps requests short and lets a partial failure recover cheaply.
_BATCH = 16

# Query-embedding cache: recall embeds the same query repeatedly during eval /
# tuning, and the per-turn lane reuses prompts. Bounded and in-process only.
_QUERY_CACHE: dict[tuple[str, str, str], list[float] | None] = {}
_QUERY_CACHE_MAX = 512


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def model_key() -> str:
    """Provider-qualified model id stored alongside each vector."""
    return f"{config.EMBED_PROVIDER}:{config.EMBED_MODEL}"


def embedding_text(atom: dict) -> str:
    """The exact text embedded for an atom: its label plus its body.

    Label first because it is the densest summary; body follows for context.
    Falls back to the id so an atom with no text still gets a stable vector.
    """
    label = (atom.get("label") or "").strip()
    text = (atom.get("text") or "").strip()
    joined = "\n".join(p for p in (label, text) if p)
    return joined or (atom.get("id") or "")


def normalize(values) -> list[float] | None:
    """L2-normalise a vector; returns None for a zero/near-zero vector."""
    norm = math.sqrt(sum(float(v) * float(v) for v in values))
    if norm <= 1e-12:
        return None
    return [float(v) / norm for v in values]


def pack(vec) -> bytes:
    return array("f", [float(v) for v in vec]).tobytes()


def unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def dot(a, b) -> float:
    """Cosine similarity of two (already normalised) vectors."""
    return math.fsum(float(x) * float(y) for x, y in zip(a, b))


# --- provider clients ---------------------------------------------------

def _post_json(url: str, payload: dict, headers: dict | None = None):
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=config.EMBED_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _embed_ollama(prepared: list[str]):
    """Ollama: batch ``/api/embed``, falling back to per-text ``/api/embeddings``."""
    try:
        payload = _post_json(
            config.EMBED_URL + "/api/embed", {"model": config.EMBED_MODEL, "input": prepared}
        )
        embs = payload.get("embeddings")
        if embs and len(embs) == len(prepared):
            return embs
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    try:
        out = []
        for t in prepared:
            payload = _post_json(
                config.EMBED_URL + "/api/embeddings", {"model": config.EMBED_MODEL, "prompt": t}
            )
            emb = payload.get("embedding")
            if not emb:
                return None
            out.append(emb)
        return out
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def _embed_litellm(prepared: list[str]):
    """LiteLLM (OpenAI-compatible) ``POST /embeddings``."""
    headers = {}
    key = config.embed_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        payload = _post_json(
            config.EMBED_URL + "/embeddings",
            {"model": config.EMBED_MODEL, "input": prepared},
            headers,
        )
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    items = payload.get("data") or []
    if len(items) != len(prepared):
        return None
    if all(isinstance(x.get("index"), int) for x in items):
        items = sorted(items, key=lambda x: x["index"])
    out = []
    for x in items:
        emb = x.get("embedding")
        if not emb:
            return None
        out.append(emb)
    return out


def embed(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts. Returns None on any provider failure (never
    raises), so callers degrade to lexical-only."""
    if not config.EMBED_ENABLED or not texts:
        return None
    prepared = [t if (t and t.strip()) else " " for t in texts]
    # Only an explicit ``litellm`` selection reaches the gateway; everything
    # else (including an unknown value) stays on the local Ollama path, so there
    # is no accidental off-host/Google dependency.
    if config.EMBED_PROVIDER == "litellm":
        return _embed_litellm(prepared)
    return _embed_ollama(prepared)


def embed_query(text: str) -> list[float] | None:
    """Normalised query vector (cached), or None when the provider is down."""
    key = (config.EMBED_PROVIDER, config.EMBED_MODEL, text or "")
    if key in _QUERY_CACHE:
        return _QUERY_CACHE[key]
    embs = embed([text or " "])
    vec = normalize(embs[0]) if embs else None
    if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
        _QUERY_CACHE.clear()
    _QUERY_CACHE[key] = vec
    return vec


def clear_cache() -> None:
    _QUERY_CACHE.clear()


def available() -> bool:
    """Cheap probe: is the configured provider serving embeddings?"""
    return embed(["ping"]) is not None


# --- store --------------------------------------------------------------

def load_vectors(conn: sqlite3.Connection, *, model: str | None = None) -> dict[str, array]:
    """atom_id -> normalised float32 vector, for the configured model key."""
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT atom_id, vec FROM m_embeddings WHERE model=?",
            (model or model_key(),),
        )
        return {r[0]: unpack(r[1]) for r in rows}
    except sqlite3.Error:
        return {}


def embedding_hash(key: str, text: str) -> str:
    return content_hash(f"{key}:{text}")


def _store(conn: sqlite3.Connection, atom_id: str, raw, key: str, text: str) -> bool:
    vec = normalize(raw)
    if vec is None:
        return False
    conn.execute(
        """INSERT OR REPLACE INTO m_embeddings(atom_id,model,dim,hash,vec,updated)
           VALUES(?,?,?,?,?,?)""",
        (atom_id, key, len(vec), embedding_hash(key, text), pack(vec), now_ms()),
    )
    return True


def _commit(conn: sqlite3.Connection) -> None:
    try:
        conn.commit()
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def embed_atom(conn: sqlite3.Connection, atom_id: str) -> bool:
    """Best-effort embedding of one atom (used by ``remember``).

    Returns True when a vector was stored. Never raises: a missing atom, a
    locked store or an unreachable provider all degrade to a no-op so the
    memory write itself cannot fail because of embeddings.
    """
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT id,label,text FROM m_atoms WHERE id=?", (atom_id,)
        ).fetchone()
        if not row:
            return False
        atom = {"id": row[0], "label": row[1], "text": row[2]}
        text = embedding_text(atom)
        embs = embed([text])
        if not embs:
            return False
        ok = _store(conn, atom_id, embs[0], model_key(), text)
        _commit(conn)
        return ok
    except Exception:  # noqa: BLE001 - must never propagate into a memory write
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return False


def embed_ids(conn: sqlite3.Connection, ids: list[str]) -> int:
    """Best-effort batch embedding of *specific* atoms (dream-pass promotions).

    The ingest loop's ``embed_missing`` would eventually pick these up, but a
    promoted memory should be semantically reachable immediately, so the dream
    pass calls this for exactly the atoms it just wrote. Bounded to one or two
    provider round-trips and never raises: any failure degrades to "the next
    ingest run embeds them", never to a failed consolidation.
    """
    try:
        ensure_schema(conn)
        key = model_key()
        rows = []
        for aid in ids:
            r = conn.execute("SELECT id,label,text FROM m_atoms WHERE id=?", (aid,)).fetchone()
            if r:
                rows.append((r[0], embedding_text({"id": r[0], "label": r[1], "text": r[2]})))
        if not rows:
            return 0
        out: list = []
        for i in range(0, len(rows), _BATCH):
            chunk = [t for _aid, t in rows[i:i + _BATCH]]
            embs = embed(chunk)
            if not embs or len(embs) != len(chunk):
                break  # provider down / partial: the ingest loop retries later
            out.extend(embs)
        stored = 0
        for (aid, text), raw in zip(rows, out):
            if _store(conn, aid, raw, key, text):
                stored += 1
        if stored:
            _commit(conn)
        return stored
    except Exception:  # noqa: BLE001 - must never propagate into a memory write
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0


def backfill(conn: sqlite3.Connection, *, force: bool = False, limit: int | None = None,
             progress=None) -> dict:
    """Embed every atom whose content hash changed (idempotent).

    Keyed by ``(model_key, hash)`` so an edited atom (or a provider/model
    switch) is re-embedded and unchanged atoms are skipped. ``force``
    re-embeds everything; ``limit`` caps one run (the ingest loop calls this with
    a small budget and finishes the backlog over time).
    """
    ensure_schema(conn)
    key = model_key()
    existing = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT atom_id, hash, model FROM m_embeddings")
    }
    atoms = [
        {"id": r[0], "label": r[1], "text": r[2]}
        for r in conn.execute("SELECT id,label,text FROM m_atoms")
    ]
    live_ids = {a["id"] for a in atoms}

    # prune embeddings for atoms that no longer exist (import_mind rebuilds docs)
    stale = [aid for aid in existing if aid not in live_ids]
    for aid in stale:
        conn.execute("DELETE FROM m_embeddings WHERE atom_id=?", (aid,))
    if stale:
        _commit(conn)

    todo: list[tuple[str, str]] = []
    for a in atoms:
        text = embedding_text(a)
        h = embedding_hash(key, text)
        prev = existing.get(a["id"])
        if force or not prev or prev[0] != h or prev[1] != key:
            todo.append((a["id"], text))
    if limit is not None and limit >= 0:
        todo = todo[:limit]

    embedded = failed = 0
    for i in range(0, len(todo), _BATCH):
        chunk = todo[i:i + _BATCH]
        embs = embed([t for _aid, t in chunk])
        if embs and len(embs) == len(chunk):
            for (aid, text), raw in zip(chunk, embs):
                if _store(conn, aid, raw, key, text):
                    embedded += 1
                else:
                    failed += 1
            _commit(conn)
            if progress:
                progress(embedded, len(todo))
            continue
        # Batch failed: retry individually so one bad input cannot sink the run.
        recovered = 0
        for aid, text in chunk:
            one = embed([text])
            if not one:
                failed += 1
                continue
            if _store(conn, aid, one[0], key, text):
                embedded += 1
                recovered += 1
            else:
                failed += 1
        _commit(conn)
        if progress:
            progress(embedded, len(todo))
        if recovered == 0:
            # Provider looks unreachable: stop; the rest stays pending.
            failed += len(todo) - (i + len(chunk))
            break

    return {
        "provider": config.EMBED_PROVIDER,
        "model": config.EMBED_MODEL,
        "model_key": key,
        "total": len(atoms),
        "embedded": embedded,
        "unchanged": max(0, len(atoms) - len(todo)),
        "pending": max(0, len(todo) - embedded - failed),
        "pruned": len(stale),
        "failed": failed,
    }


def embed_missing(conn: sqlite3.Connection, budget: int = 24) -> dict:
    """Small, best-effort top-up used by the ingest loop. A hard cap keeps a
    first big backlog from stretching one ingest run; later runs continue."""
    try:
        return backfill(conn, limit=budget)
    except sqlite3.Error:
        return {"embedded": 0, "failed": 0, "error": "store"}
