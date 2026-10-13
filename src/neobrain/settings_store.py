"""Runtime settings for the Observatory Settings tab.

Env vars (``.env``) remain the source of defaults; this module layers
**DB-backed overrides** (``app_settings``, schema v5) on top and lets the UI
read/write them. ``config.apply_overrides`` pushes a change into the live
``settings`` singleton and recomputes the derived globals, so every key takes
effect immediately — including the embeddings provider/URL/model/key, which
``config._derive_embeddings`` rebuilds and ``embeddings.py`` reads per call.
Only ``bind``/``api`` (the socket is already listening) and ``data_dir`` (the
store path is resolved at import) are marked restart-required; the UI warns on
save instead of tagging the field.

Two rules the whole surface obeys:

* a **secret** is never returned — the API reports only whether one is set;
* a write that omits a secret leaves the stored one untouched (so the UI can
  round-trip the form without holding the key).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from . import config
from .models import now_ms


@dataclass(frozen=True)
class Setting:
    """One editable setting, with the metadata the UI renders it from."""

    key: str
    label: str
    kind: str  # bool | int | float | str | csv | time | secret | choice
    section: str
    help: str = ""
    choices: tuple[str, ...] = ()
    restart: bool = False   # needs a daemon restart to take effect
    env_only: bool = False  # shown read-only; edit .env instead
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    suffix: str = ""


SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("features", "Features & rituals", "What the brain does on its own, and whether it may."),
    ("schedule", "Schedule & quiet hours", "When the life loop and its rituals run."),
    ("cognition", "Cognition — LLM provider", "Where the brain's own thoughts are generated."),
    ("memory", "Memory & forgetting", "Rank and recall tuning (SPEC §4.2)."),
    ("embeddings", "Embeddings", "Vector provider. The local default keeps memory text on-LAN."),
    ("notifications", "Notifications", "Where stall and health alerts are sent."),
    ("sources", "Sources", "What the brain reads: workspace docs, repos, OpenCode sessions."),
    ("daemon", "Daemon", "Bind address and data directory."),
)

#: Known provider presets the Cognition section offers (label, base URL).
PROVIDERS: tuple[dict[str, str], ...] = (
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"id": "custom", "label": "Custom…", "base_url": ""},
)


REGISTRY: tuple[Setting, ...] = (
    # --- features ---------------------------------------------------------
    Setting("life_enabled", "Life loop", "bool", "features",
            "The master switch. Off stops perceive/reflect/act/dreams entirely."),
    Setting("dreams_enabled", "Dreams", "bool", "features",
            "Nightly consolidation, narrative and lessons at the dream hour."),
    Setting("soul_reflect_enabled", "Identity reflection", "bool", "features",
            "The weekly pass that reviews and evolves SOUL.md / IDENTITY.md."),
    Setting("consolidate_enabled", "Consolidation", "bool", "features",
            "The reflect phase: merge hubs, promote patterns, fold in day notes."),
    Setting("act_enabled", "Internal thoughts", "bool", "features",
            "The act phase: one private first-person observation per interval."),
    Setting("rating_watch_enabled", "Rating watch", "bool", "features",
            "A daily memory-feedback snapshot that pings you if ratings stall."),
    # --- schedule ---------------------------------------------------------
    Setting("life_tick_seconds", "Loop tick", "int", "schedule", "Seconds between life-loop passes.",
            minimum=5, maximum=3600, suffix="s"),
    Setting("life_perceive_interval_minutes", "Perceive every", "int", "schedule",
            "Health probes + ingest.", minimum=1, maximum=1440, suffix="min"),
    Setting("life_reflect_interval_minutes", "Reflect every", "int", "schedule",
            "Consolidation cadence.", minimum=1, maximum=1440, suffix="min"),
    Setting("life_act_interval_minutes", "Act every", "int", "schedule",
            "Internal-thought cadence.", minimum=1, maximum=1440, suffix="min"),
    Setting("life_dream_hour", "Dream hour", "int", "schedule",
            "Local hour the nightly dream pass may run.", minimum=0, maximum=23, suffix=":00"),
    Setting("reflect_weekdays", "Reflection days", "csv", "schedule",
            "0=Mon … 6=Sun, comma-separated. Default Mon/Wed/Sat = 0,2,5."),
    Setting("life_quiet_start", "Quiet from", "int", "schedule",
            "Local hour quiet hours begin (only perceive runs).", minimum=0, maximum=23, suffix=":00"),
    Setting("life_quiet_end", "Quiet until", "int", "schedule",
            "Local hour quiet hours end.", minimum=0, maximum=23, suffix=":00"),
    Setting("rating_watch_hour", "Rating watch hour", "int", "schedule",
            "Local hour the daily rating snapshot runs.", minimum=0, maximum=23, suffix=":00"),
    Setting("rating_watch_days", "Rating watch window", "int", "schedule",
            "Look-back window for the snapshot.", minimum=1, maximum=90, suffix="d"),
    Setting("rating_watch_until", "Rating watch ends", "str", "schedule",
            "ISO date (YYYY-MM-DD); the watch stops after it. Empty = no end."),
    # --- cognition --------------------------------------------------------
    Setting("llm_base_url", "Base URL", "str", "cognition",
            "Any OpenAI-compatible endpoint, e.g. https://api.openai.com/v1."),
    Setting("llm_api_key", "API key", "secret", "cognition",
            "Bearer key for the endpoint. Leave blank to keep the current one."),
    Setting("llm_model_cheap", "Cheap model", "str", "cognition",
            "Perceive/reflect tier — the everyday model."),
    Setting("llm_model_strong", "Strong model", "str", "cognition",
            "Act/dream tier — the deeper model."),
    # --- memory -----------------------------------------------------------
    Setting("recall_alpha", "Recall vector weight", "float", "memory",
            "score = lexical + alpha·cosine. Higher leans on embeddings.",
            minimum=0.0, maximum=10.0),
    Setting("rank_half_life_days", "Forgetting half-life", "float", "memory",
            "Days for recency decay to halve.", minimum=0.5, maximum=365.0, suffix="d"),
    Setting("rank_decay_floor", "Recency floor", "float", "memory",
            "Recency decay never drops below this.", minimum=0.0, maximum=1.0),
    Setting("rank_conn_floor", "Connectivity floor", "float", "memory",
            "Graph connectivity never drops below this.", minimum=0.0, maximum=1.0),
    Setting("rank_prior_n", "Feedback prior", "float", "memory",
            "Smoothing strength for the average feedback.", minimum=0.0, maximum=100.0),
    Setting("rank_ignore_below", "Forget below", "float", "memory",
            "Quality under this becomes a forgetting candidate.", minimum=0.0, maximum=1.0),
    Setting("rank_archive_exposure", "Archive exposure", "int", "memory",
            "Low quality + exposure over this → archived.", minimum=0, maximum=1000),
    Setting("reflect_context_chars", "Reflect context budget", "int", "memory",
            "Char budget for evidence inlined into the reflection prompt.",
            minimum=1000, maximum=200000),
    Setting("dreams_file", "Dreams file", "str", "memory",
            "Where DREAMS.md lives. Empty = WORKSPACE/DREAMS.md."),
    # --- embeddings -------------------------------------------------------
    Setting("embed_provider", "Provider", "choice", "embeddings",
            "Switching provider re-embeds everything (the vector key changes).",
            choices=("ollama", "litellm")),
    Setting("embed_enabled", "Enable embeddings", "bool", "embeddings",
            "Off = lexical-only recall."),
    Setting("embed_url", "Endpoint", "str", "embeddings",
            "Base URL of the embeddings endpoint (blank = provider default)."),
    Setting("embed_model", "Model", "str", "embeddings",
            "Embedding model id (blank = provider default)."),
    Setting("embed_api_key", "API key", "secret", "embeddings",
            "Key for the embeddings endpoint. Blank keeps the current one."),
    # --- notifications ----------------------------------------------------
    Setting("notify_url", "Notify URL", "str", "notifications",
            "Local notifyd endpoint. Empty disables pings."),
    Setting("notify_secret", "Notify secret", "secret", "notifications",
            "Bearer secret for notifyd. Blank keeps the current one."),
    # --- sources ----------------------------------------------------------
    Setting("workspace", "Workspace", "str", "sources",
            "Root the brain scans for docs and repos."),
    Setting("known_repos", "Known repos", "csv", "sources",
            "Comma-separated repos under the workspace, checked first."),
    Setting("opencode_db", "OpenCode DB", "str", "sources",
            "Path to the OpenCode session database. Empty = derive from workspace."),
    # --- daemon -----------------------------------------------------------
    Setting("bind", "Bind", "str", "daemon",
            "host:port the daemon listens on.", restart=True),
    Setting("api", "API URL", "str", "daemon",
            "URL shown to clients (the daemon's own address).", restart=True),
    Setting("data_dir", "Data directory", "str", "daemon",
            "Where the store lives. Change it in .env, then restart.",
            restart=True, env_only=True),
)

_BY_KEY: dict[str, Setting] = {s.key: s for s in REGISTRY}


# --- read ---------------------------------------------------------------


def _rows(conn: sqlite3.Connection) -> dict[str, Any]:
    """Typed override values currently stored ({} when the table is absent)."""
    try:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, Any] = {}
    for row in rows:
        key, raw = row[0], row[1]
        if key not in _BY_KEY:
            continue
        try:
            out[key] = config.coerce_field(key, json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return out


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything the Settings tab needs to render: sections + fields."""
    overrides = _rows(conn)
    defaults = config.Settings()  # env + .env only (no DB overrides)
    out_sections: list[dict[str, Any]] = []
    for sid, label, description in SECTIONS:
        fields: list[dict[str, Any]] = []
        for spec in REGISTRY:
            if spec.section != sid:
                continue
            effective = getattr(config.settings, spec.key, None)
            default = getattr(defaults, spec.key, None)
            secret = spec.kind == "secret"
            item: dict[str, Any] = {
                "key": spec.key,
                "label": spec.label,
                "kind": spec.kind,
                "help": spec.help,
                "choices": list(spec.choices),
                "restart": spec.restart,
                "env_only": spec.env_only,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "suffix": spec.suffix,
                "secret": secret,
                "overridden": spec.key in overrides,
                "from_env": f"NEOBRAIN_{spec.key.upper()}" in os.environ,
                "value": "" if secret else _jsonable(effective),
                "default": "" if secret else _jsonable(default),
                "set": bool(str(effective or "").strip()) if secret else None,
            }
            fields.append(item)
        out_sections.append({"id": sid, "label": label, "description": description, "fields": fields})
    return {
        "sections": out_sections,
        "providers": list(PROVIDERS),
        "life_enabled": str(config.settings.life_enabled).strip().lower()
        not in ("0", "false", "no", "off"),
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def restart_pending(conn: sqlite3.Connection) -> list[str]:
    """Overridden keys that only take effect after a daemon restart."""
    return [k for k in _rows(conn) if _BY_KEY[k].restart]


# --- write --------------------------------------------------------------


def _field_is_bool(key: str) -> bool:
    return config.Settings.model_fields[key].annotation is bool


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def set_values(conn: sqlite3.Connection, values: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update. ``None`` reverts a key to its env default.

    A secret sent as an empty string is a no-op (keeps the stored value), so the
    UI can submit a form without ever holding the key. Unknown keys and
    ``env_only`` fields raise ``ValueError``; bad values raise ``ValueError``.
    """
    before = set(_rows(conn))
    applied: list[str] = []
    reverted: list[str] = []
    for key, value in values.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"unknown setting: {key}")
        if spec.env_only:
            raise ValueError(f"{key} is env-only — edit .env and restart")
        if spec.kind == "secret" and isinstance(value, str) and not value.strip():
            continue  # blank secret = keep the current one
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
            reverted.append(key)
            continue
        try:
            if spec.kind == "bool":
                flag = _truthy(value)
                coerced = flag if _field_is_bool(key) else ("1" if flag else "0")
            else:
                coerced = config.coerce_field(key, value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{key}: {exc}") from exc
        if spec.minimum is not None and float(coerced) < spec.minimum:
            raise ValueError(f"{key}: below minimum {spec.minimum}")
        if spec.maximum is not None and float(coerced) > spec.maximum:
            raise ValueError(f"{key}: above maximum {spec.maximum}")
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(coerced), now_ms()),
        )
        applied.append(key)
    conn.commit()
    # Push the new effective values into the live settings singleton. Reload the
    # whole override set so a revert and an apply in one request cannot drift.
    overrides = config.db_overrides()
    # A reverted key must fall back to its env default on the live singleton —
    # apply_overrides only *sets* keys, it never clears them.
    removed = before - set(overrides)
    if removed:
        defaults = config.Settings()
        for key in removed:
            if key in _BY_KEY:
                setattr(config.settings, key, getattr(defaults, key))
    config.apply_overrides(overrides)
    return {
        "applied": applied,
        "reverted": reverted,
        "restart_pending": restart_pending(conn),
    }


# --- provider discovery / probe ----------------------------------------


def _bearer(api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def effective_key(api_key: Optional[str]) -> str:
    """The key to use: an explicit one, else the stored LLM key (never logged)."""
    if api_key and api_key.strip():
        return api_key.strip()
    return (config.settings.llm_api_key or "").strip()


def discover_models(base_url: str, api_key: str, timeout: float = 10.0) -> list[str]:
    """Model ids advertised by an OpenAI-compatible ``/models`` endpoint."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers=_bearer(api_key), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-supplied endpoint
        payload = json.loads(resp.read().decode("utf-8"))
    items = payload.get("data") or payload.get("models") or []
    ids: set[str] = set()
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                ids.add(str(mid))
        elif isinstance(it, str):
            ids.add(it)
    return sorted(ids)


def probe_model(base_url: str, api_key: str, model: str, timeout: float = 30.0) -> dict[str, Any]:
    """One tiny chat completion, to prove an endpoint + key + model work."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 16,
            "temperature": 0,
        }
    ).encode("utf-8")
    headers = _bearer(api_key)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-supplied endpoint
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = " ".join(exc.read().decode("utf-8", "replace").split())[:300]
        except Exception:  # noqa: BLE001 - the body is diagnostic only
            pass
        return {"ok": False, "error": f"HTTP {exc.code}" + (f": {detail}" if detail else "")}
    except Exception as exc:  # noqa: BLE001 - a probe failure is data, not a crash
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    latency = round((time.perf_counter() - started) * 1000)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = ""
    return {"ok": True, "latency_ms": latency, "model": payload.get("model") or model, "sample": (text or "").strip()[:200]}
