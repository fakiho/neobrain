"""neoBrain — configuration and source discovery (pydantic settings).

All environment variables are prefixed ``NEOBRAIN_`` (the old ``TIMELINE_*``
names). Host-specific paths that the old config hardcoded (/home/sparo) are
now env-driven with empty/sensible defaults — see the PORT-NOTEs below.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    # PORT-NOTE: the old config loaded .env from the package's repo root
    # (app/..). As an installed package that path is site-packages, so the
    # only stable location is the current working directory.
    load_dotenv(Path.cwd() / ".env")
except Exception:  # pragma: no cover - dotenv is optional for ingest
    pass


# The CLI must resolve the same mind as the daemon: read the repo .env from
# any CWD (before this, a bare `neobrain remember` outside the daemon env
# silently wrote to a stray <cwd>/data/neobrain.db). Real environment
# variables still take precedence over the file.
_REPO_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """neoBrain configuration, read from ``NEOBRAIN_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="NEOBRAIN_",
        extra="ignore",
        env_file=str(_REPO_ENV) if _REPO_ENV.exists() else None,
    )

    # --- data ---
    # PORT-NOTE: old default was <repo>/data; now cwd-relative per SPEC S1.
    data_dir: Path = Path("./data")

    # PORT-NOTE: old default was the hardcoded /home/sparo workspace. Empty
    # means "no workspace": doc groups are empty and repo discovery is off.
    workspace: str = ""

    # --- API / dashboard ---
    api: str = "http://127.0.0.1:9192"

    # --- embeddings (local-first default) ---
    embed_provider: str = "ollama"
    embed_url: str | None = None      # None -> per-provider default below
    embed_model: str | None = None    # None -> per-provider default below
    embed_timeout: float | None = None
    embed_enabled: str = "1"
    # PORT-NOTE: old default was $WORKSPACE/.openclaw/openclaw.json (a
    # /home/sparo path). Now unset by default; only read when configured.
    embed_key_file: str | None = None
    embed_api_key: str = ""

    # --- recall ---
    recall_alpha: float = 2.0

    # --- rank (deterministic; SPEC §4.2/4.3 — the model has no discretion) ---
    # quality = feedback * decay(recency) * conn(connectivity); see rank.py.
    # Each factor is bounded in [floor, 1] so every one of them can push a
    # memory under the forgetting threshold — including model feedback alone.
    rank_decay_floor: float = 0.35   # recency decay never drops below this
    rank_conn_floor: float = 0.5     # graph connectivity never drops below this
    rank_half_life_days: float = 21.0
    rank_prior_n: float = 10.0       # smoothing prior strength for feedback avg
    rank_ignore_below: float = 0.15  # quality under this = forgetting candidate
    rank_archive_exposure: int = 5   # exposure score over this + low quality -> archived

    # --- LLM runtime (OpenAI-compatible) ---
    llm_base_url: str = "http://127.0.0.1:4000/v1"
    llm_api_key: str = ""
    # PORT-NOTE (S5 review): the house LiteLLM gateway has no
    # "deepseek/deepseek-flash" — the cheap tier is v4.1-flash (verified against
    # /v1/models). Reasoning models spend a small max_tokens budget on hidden
    # reasoning and return empty content, so give them a real budget.
    llm_model_cheap: str = "deepseek/deepseek-v4.1-flash"
    llm_model_strong: str = "deepseek/deepseek-v4-pro"

    # --- life loop (SPEC §6: cadence in code+DB, not OS timers) ---
    life_tick_seconds: int = 60
    life_quiet_start: int = 23       # local hour: only perceive during quiet hours
    life_quiet_end: int = 8
    life_perceive_interval_minutes: int = 15
    life_reflect_interval_minutes: int = 30
    life_act_interval_minutes: int = 240
    life_dream_hour: int = 2         # local hour rest() may invoke the dream phases
    life_enabled: str = "1"          # "0" disables the in-daemon life loop

    # --- dreams / reflect (SPEC §4.4/4.5) ---
    # Empty -> WORKSPACE/DREAMS.md when a workspace is set, else DATA_DIR/DREAMS.md.
    dreams_file: str = ""
    reflect_weekdays: str = "0,2,5"  # 0=Mon … 6=Sun; soul-reflect days (Mon/Wed/Sat, user set 2026-09-30)
    # Char budget for the evidence the reflect pass inlines (DREAMS.md + the
    # recent memory/dreaming/rem notes). Newest entries stay whole; older ones
    # keep their id line and have only their body compacted, so the prompt
    # cannot blow the context window without dropping a referenceable id.
    reflect_context_chars: int = 12000

    # --- rating watch (bounded observation of memory-rating volume) ---
    # Rides the existing in-process life loop — no OS timer. Once a day at
    # ``rating_watch_hour`` the loop records a rating-volume snapshot and, if
    # agent ratings have stalled at zero, pings the local notifier. Temporary
    # by design: set ``rating_watch_until`` to an end date and it stops after it.
    rating_watch_enabled: str = "0"   # "1" enables the daily snapshot
    rating_watch_days: int = 7        # look-back window for the snapshot
    rating_watch_hour: int = 9        # local hour to run (a waking hour, so a stall alert is not a 2am ping)
    rating_watch_until: str = ""      # ISO date (local); empty = no end; the watch stops after this day
    notify_url: str = "http://127.0.0.1:8788/notify"  # local notifyd; empty disables the ping
    notify_secret: str = ""           # Bearer secret; empty -> read HTTP_SECRET from ~/.config/notify/config

    # --- daemon bind (API + dashboard) ---
    bind: str = "0.0.0.0:9192"

    # --- sources (workspace docs, repos, opencode session DB) ---
    # PORT-NOTE: the old KNOWN_REPOS was a hardcoded host-specific list; now a
    # comma-separated env var, empty by default (os.walk discovery still runs).
    known_repos: str = ""
    # PORT-NOTE: old default was $WORKSPACE/.local/share/opencode/opencode.db
    # (under hardcoded /home/sparo). Now derived from NEOBRAIN_WORKSPACE when
    # set, else None.
    opencode_db: str | None = None


settings = Settings()


def reflect_days() -> set[int]:
    """Weekdays (0=Mon … 6=Sun) the soul-reflect may run on (SPEC §4.5)."""
    days = {
        int(p.strip()) for p in settings.reflect_weekdays.split(",")
        if p.strip().isdigit() and int(p.strip()) <= 6
    }
    return days or {6}

# --- resolved paths -----------------------------------------------------
DATA_DIR = settings.data_dir.expanduser().resolve()
# PORT-NOTE: DB renamed timeline.db -> neobrain.db (S1 contract).
DB_PATH = DATA_DIR / "neobrain.db"

# PORT-NOTE: empty NEOBRAIN_WORKSPACE must NOT resolve to the cwd (Path("").resolve()
# would), so an unset workspace stays None and doc groups are empty.
WORKSPACE: Path | None = Path(settings.workspace).expanduser().resolve() if settings.workspace.strip() else None

OPENCODE_DB: Path | None = (
    Path(settings.opencode_db).expanduser()
    if settings.opencode_db
    else (WORKSPACE / ".local/share/opencode/opencode.db") if WORKSPACE else None
)

# --- embeddings (provider-configurable) --------------------------------
# `ollama` (default): `embeddinggemma` served on the host — memory text never
#   leaves the machine. 768-dim, 2048-token context; CPU inference is slower
#   than a cloud embedder, hence the generous timeout.
# `litellm`: Google `gemini-embedding-2` (3072-dim) reached through the local
#   LiteLLM gateway. NOTE: this sends memory text off-host to Google — approved
#   by the user 2026-09-27 (see INFRA_TIMELINE.md §4a). Kept as an option.
# Switching provider re-embeds everything (the provider is part of the vector's
# model key), so both paths can coexist as an option.
_EMBED_PROVIDER = settings.embed_provider.strip().lower()
_EMBED_DEFAULTS = {
    "ollama": ("http://127.0.0.1:11434", "embeddinggemma", 180.0),
    "litellm": ("http://127.0.0.1:4000/v1", "gemini-embedding-2", 30.0),
}
_default_url, _default_model, _default_timeout = _EMBED_DEFAULTS.get(
    _EMBED_PROVIDER, _EMBED_DEFAULTS["ollama"]
)
EMBED_PROVIDER = _EMBED_PROVIDER
EMBED_URL = (settings.embed_url or _default_url).rstrip("/")
EMBED_MODEL = settings.embed_model or _default_model
EMBED_TIMEOUT = settings.embed_timeout if settings.embed_timeout is not None else _default_timeout
EMBED_ENABLED = settings.embed_enabled.strip().lower() not in ("0", "false", "no", "off")
EMBED_KEY_FILE: Path | None = Path(settings.embed_key_file).expanduser() if settings.embed_key_file else None

# Hybrid recall weight: score = lexical_norm + alpha * cosine_norm (both 0..1).
# Provider-agnostic because of the min-max norm.
RECALL_ALPHA = settings.recall_alpha

_embed_key: str | None = None


def embed_api_key() -> str:
    """The LiteLLM gateway key (never logged). Env overrides the local config."""
    global _embed_key
    if _embed_key is not None:
        return _embed_key
    for name in ("NEOBRAIN_EMBED_API_KEY", "LITELLM_API_KEY"):
        value = os.environ.get(name)
        if value:
            _embed_key = value
            return value
    if EMBED_KEY_FILE is None:
        _embed_key = ""  # PORT-NOTE: no key file configured -> no key to read
    else:
        try:
            data = json.loads(EMBED_KEY_FILE.read_text(encoding="utf-8"))
            _embed_key = data["models"]["providers"]["litellm"]["apiKey"] or ""
        except Exception:  # missing/invalid file, or no key configured
            _embed_key = ""
    return _embed_key

# --- docs ---------------------------------------------------------------
# Explicit, ordered doc groups. `category` becomes the event category.
#
# Groups are globbed *on demand* (not at import): a process that starts at 00:09
# must still see the daily note written at 00:20 — a startup-time snapshot left
# new `memory/*.md` and `INFRA_*.md` files invisible to both the reader tabs and
# the ingest loop until a restart. `DOC_GROUPS` is kept as an import-time
# snapshot for direct importers; prefer `doc_groups()` for a live view.
def doc_groups() -> dict[str, list[Path]]:
    """Doc groups, re-globbed on each call so new files are picked up."""
    # PORT-NOTE: empty NEOBRAIN_WORKSPACE -> no docs (the old code defaulted
    # the workspace to /home/sparo and globbed it unconditionally).
    if WORKSPACE is None:
        return {}
    dreaming = sorted((WORKSPACE / "memory" / "dreaming").glob("**/*.md"))
    dreams_md = WORKSPACE / "DREAMS.md"
    return {
        "infra": sorted(WORKSPACE.glob("INFRA_*.md")),
        "identity": [
            p
            for p in (
                WORKSPACE / "AGENTS.md",
                WORKSPACE / "USER.md",
                WORKSPACE / "SOUL.md",
                WORKSPACE / "IDENTITY.md",
            )
            if p.exists()
        ],
        "memory": sorted((WORKSPACE / "memory").glob("*.md")),
        "dream": ([dreams_md] if dreams_md.exists() else []) + dreaming,
    }


DOC_GROUPS: dict[str, list[Path]] = doc_groups()


def all_docs() -> list[tuple[str, Path]]:
    """Return (category, path) for every tracked doc (live view)."""
    out: list[tuple[str, Path]] = []
    for category, paths in doc_groups().items():
        for p in paths:
            out.append((category, p))
    return out


# --- repos --------------------------------------------------------------
KNOWN_REPOS = [r.strip() for r in settings.known_repos.split(",") if r.strip()]

_SKIP_DIRS = {
    ".cache", ".local", ".config", ".var", ".npm", ".nvm", "node_modules",
    ".git", "Trash", "snap", ".vscode-server", ".cursor-server", ".ollama",
}


def discover_repos(max_depth: int = 3) -> list[Path]:
    """Find git repos under the workspace (known list first, then discovery)."""
    # PORT-NOTE: empty NEOBRAIN_WORKSPACE -> no repos (Path("") would walk the cwd).
    if WORKSPACE is None:
        return []
    found: dict[Path, None] = {}

    for rel in KNOWN_REPOS:
        p = WORKSPACE / rel
        if (p / ".git").is_dir():
            found[p.resolve()] = None

    base_depth = len(WORKSPACE.parts)
    for root, dirs, files in os.walk(WORKSPACE):
        root_path = Path(root)
        depth = len(root_path.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        if (root_path / ".git").is_dir():
            found[root_path.resolve()] = None

    return sorted(found)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
