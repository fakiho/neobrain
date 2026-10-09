"""Mind model — atoms, hubs and typed edges built from the agent's real notes.

Sources: memory/*.md, DREAMS.md, memory/dreaming/**, INFRA_*.md and the identity
docs. This is the M1 importer: heuristic, deterministic and idempotent (full
rebuild on each run). The agent's own curating tools come later (M2).
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# PORT-NOTE: S4 — rank.py is the deterministic rank/forgetting engine; the
# schema migration is needed so any mind op upgrades a legacy store lazily.
from . import config, embeddings, rank
from .heuristics import classify_heading
from .models import content_hash, now_ms
from .schema import SCHEMA, migrate as _schema_migrate

# PORT-NOTE: `sections` used to come from app.ingest.docs; ingest/ is not
# ported in S1, so the function is inlined here verbatim (logic unchanged).
_HEADING = re.compile(r"^(#{2,3})\s+(.*\S)\s*$")


def sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) for level-2/3 headings."""
    out: list[tuple[str, str]] = []
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            if current is not None:
                out.append((current, "\n".join(buf).strip()))
            current = m.group(2).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out.append((current, "\n".join(buf).strip()))
    return out

# PORT-NOTE: SCHEMA moved to neobrain/schema.py — one authoritative schema
# module (events/ingest + mind + embeddings DDL merged). Applying the full
# merged schema here is behaviorally identical: every statement is
# `CREATE ... IF NOT EXISTS`, so lazy _ensure_schema() stays idempotent.
#

# entity dictionary: hub id -> (label, keywords)
HUBS: dict[str, tuple[str, list[str]]] = {
    "openwrt": ("OpenWrt", ["openwrt", "luci", "dnsmasq", "dhcp", "lan2", "testlan", " nft", "odhcpd", "apk ", "bridge", "br-lan"]),
    "dns": ("DNS", ["adguard", "unbound", "dns", "doh", "dot ", "port 53", "ad-block", "adblock", "resolver"]),
    "freebox": ("Freebox", ["freebox"]),
    "frigate": ("Frigate", ["frigate", "nvr", "detect", "recordings", "go2rtc", "homekit", "g2h", "aqara", "camera", "stream"]),
    "ha": ("Home Assistant", ["home assistant", " hass", "ha ", "matter", "thread", "zigbee", "lovelace", "nova", "automation"]),
    "grafana": ("Grafana", ["grafana", "dashboard", "panel", "influxql"]),
    "influxdb": ("InfluxDB", ["influxdb", "telegraf", "bucket", "measurement", "influx"]),
    "litellm": ("LiteLLM", ["litellm", "llm gateway", "model", "router"]),
    "opencode": ("OpenCode", ["opencode", "opencode.service", " pair", "subagent", "agent web", "tool call"]),
    "node-red": ("Node-RED", ["node-red", "nodered", "flows.json", "telegram"]),
    "host": ("Host", ["host ", "nvme", "eno2", "br0", "systemd", "docker", "swap", "pcie", "kernel", "networkmanager", "wlo1"]),
    "finance": ("Finance", ["finance", "fakihofinance", "financefakih"]),
    "neohive": ("neohive", ["neohive"]),
    "proxy": ("Proxy lab", ["proxy-inspector", "mitmproxy"]),
    "iot": ("IoT", ["iot", "iot segment", "test lan", "testlan", "lan2"]),
    "agent": ("Agent", ["memory", "dream", "identity", "soul", "skill", "preference", "lesson", "learned", "observe", "timeline", "observatory"]),
}

# near-duplicate hub ids → canonical id (keeps the graph from fragmenting)
ALIASES: dict[str, str] = {
    "home-assistant": "ha", "hass": "ha", "homeassistant": "ha",
    "matter": "ha", "thread": "ha", "zigbee": "ha", "matter/thread": "ha",
    "homekit": "frigate", "go2rtc": "frigate", "nvr": "frigate", "camera": "frigate", "cameras": "frigate",
    "docker": "host", "systemd": "host", "networkmanager": "host", "pcie": "host", "nvme": "host",
    "br0": "host", "eno2": "host", "nic": "host", "snort": "host",
    "llm": "litellm", "llm-gateway": "litellm", "models": "litellm",
    "network": "openwrt", "openwrt-router": "openwrt", "dhcp": "openwrt", "dnsmasq": "openwrt", "router": "openwrt",
    "adguard": "dns", "adguardhome": "dns", "unbound": "dns", "doh": "dns", "dot": "dns", "adblock": "dns",
    "nodered": "node-red", "node_red": "node-red",
    "financefakih": "finance", "fakiho-finance": "finance", "fakihofinance": "finance",
    "graphana": "grafana", "influx": "influxdb", "telegraf": "influxdb",
    # agent-authored hubs that had drifted out of the canonical set
    "timeline": "agent", "observatory": "agent",
    "dashboard": "grafana",
    # "automation" here means the host automations (INFRA_AUTOMATION.md: scripts,
    # systemd units, cron), not Home Assistant — the HA hub is used for HA itself.
    "automation": "host",
}


def _canon(hid: str | None) -> str:
    h = (hid or "").strip().lower()
    return ALIASES.get(h, h)

_TYPE_MAP = {
    "decision": "decision",
    "change": "decision",
    "revert": "decision",
    "goal": "decision",
    "root-cause": "lesson",
    "lesson": "lesson",
    "open": "open",
}

# --- persona capture (IDENTITY.md fields, SOUL.md sections) ---------------
# IDENTITY.md stores the agent's identity as `- Label: value` lines. The label
# set is fixed, so prose that merely contains a colon is never mistaken for a
# field. Values wrapped in parentheses are unfilled placeholders → ignored.
_IDENTITY_FIELDS = ("name", "creature", "vibe", "emoji", "avatar", "theme")
_IDENTITY_FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z ]{0,24}?):\s*(.+)$")

# SOUL.md sections whose bullets become `preference` atoms (persona rules).
_SOUL_SECTIONS = ("Core Truths", "Boundaries")
# A bullet under Subagent Delegation "reads as a rule" when it carries an
# imperative/modal instruction such as "Provide…", "Name…", "read…", "Pass…".
_RULE_HINT = re.compile(
    r"\b(must|never|always|do not|don't|avoid|ensure|provide|name|read|pass|explicitly|command)\b",
    re.I,
)


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _memory_date(path: Path) -> int:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path.name)
    if m:
        # 20:00 local on that day, but never later than the file's own mtime
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 20, 0).astimezone()
        ts = int(dt.timestamp() * 1000)
        mtime = int(path.stat().st_mtime * 1000)
        return min(ts, mtime)
    return int(path.stat().st_mtime * 1000)


def _hubs_for(text: str) -> list[str]:
    low = " " + text.lower() + " "
    scores: list[tuple[int, str]] = []
    for hid, (label, kws) in HUBS.items():
        n = 0
        for k in kws:
            k = k.strip()
            if k and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low):
                n += 2
        if label.lower() in low:
            n += 3
        if n:
            scores.append((n, hid))
    scores.sort(key=lambda x: (-x[0], x[1]))
    return [h for _n, h in scores[:3]]


def _atom_type(heading: str, body: str) -> str:
    rule = classify_heading(heading)
    if rule:
        lane, category, _sev = rule
        if category in _TYPE_MAP:
            return _TYPE_MAP[category]
    if re.search(r"\b(revert|instead of|supersede|no longer)\b", body, re.I):
        return "decision"
    return "observation"


def _weight(atype: str) -> float:
    return {"lesson": 0.9, "decision": 0.8, "preference": 0.85, "dream": 0.6, "open": 0.7}.get(atype, 0.5)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- standing directives (pinned persona rules) --------------------------
# A USER.md/SOUL.md bullet carrying the literal ``[pin]`` (or an invisible HTML
# ``<!-- pin -->``) marker is a must-follow rule. It stays a `preference` atom and
# gains the ``pin`` tag — the durable flag the OpenCode adapter reads to re-inject
# it into every model call. The marker is stripped from the stored text/label; the
# tag is what persists.
_PIN_RE = re.compile(r"\s*(?:\[pin\]|<!--\s*pin\s*-->)\s*", re.I)


def _pin_tags(tags: list[str], text: str) -> tuple[list[str], str]:
    """Split the ``[pin]`` marker out of a bullet → (tags, marker-free text)."""
    if not _PIN_RE.search(text):
        return tags, text
    pinned = [*tags, "pin"] if "pin" not in tags else list(tags)
    return pinned, _clean(_PIN_RE.sub(" ", text))


def _add_atom(atoms: dict, atom: dict, edges: set, hubs_seen: set) -> None:
    atoms[atom["id"]] = atom
    for h in atom["_hubs"]:
        edges.add((atom["id"], h, "about"))
        hubs_seen.add(h)


def _parse_markdown(path: Path, category: str, atoms: dict, edges: set, hubs_seen: set) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    default_ts = _memory_date(path) if category == "memory" else int(path.stat().st_mtime * 1000)

    if category == "dream":
        if path.name != "DREAMS.md":
            # a memory/dreaming/**/*.md file: whole file is one dream entry
            ts = _memory_date(path)
            body = _clean(re.sub(r"^#.*$", "", text, flags=re.M))
            if len(body) < 20:
                return
            kind = path.parent.name  # light | deep | rem
            rel = str(path.relative_to(config.WORKSPACE))
            aid = "d_" + content_hash(rel)[:12]  # include the phase folder, else light/rem/deep collide (same filename)
            _add_atom(atoms, {
                "id": aid, "label": f"Dream ({kind}) {path.stem}"[:80], "type": "dream",
                "created": ts, "text": body,
                "source": str(path.relative_to(config.WORKSPACE)), "tags": [], "weight": 0.55,
                "hub": "agent", "_hubs": ["agent"], "hash": content_hash(text),
            }, edges, hubs_seen)
            return
        # DREAMS.md: entries separated by '---' with an italic date line
        for block in text.split("\n---"):
            m = re.search(r"\*([A-Z][a-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M[^*]*)\*", block)
            if not m:
                continue
            raw = re.sub(r"\s*GMT[+-]\d+", "", m.group(1)).strip()
            try:
                # The date line is local wall time (_append_dreams writes
                # now.strftime); parse it as local so a late-evening entry
                # groups on the night it belongs to, not the next day.
                ts = _ms(datetime.strptime(raw, "%B %d, %Y at %I:%M %p").astimezone())
            except ValueError:
                ts = default_ts
            body = _clean(block.replace(m.group(0), ""))
            if len(body) < 40:
                continue
            aid = "d_" + content_hash(path.name + m.group(1))[:12]
            label = "Dream: " + " ".join(body.split()[:5])
            _add_atom(atoms, {
                "id": aid, "label": label[:80], "type": "dream", "created": ts,
                "text": body, "source": str(path.relative_to(config.WORKSPACE)),
                "tags": [], "weight": 0.6, "hub": "agent", "_hubs": ["agent"],
                "hash": content_hash(body),
            }, edges, hubs_seen)
        return

    for heading, body in sections(text):
        if len(body) < 40:
            continue
        atype = _atom_type(heading, body)
        if category == "infra" and atype == "observation":
            continue  # keep INFRA to decisions/lessons/opens
        tags = [h for h in _hubs_for(heading + " " + body)]
        primary = tags[0] if tags else ("agent" if category in ("identity", "dream") else None)
        src = str(path.relative_to(config.WORKSPACE))
        aid = "a_" + content_hash(f"{src}:{heading}")[:12]
        _add_atom(atoms, {
            "id": aid, "label": heading[:90], "type": atype, "created": default_ts,
            "text": body[:2500], "source": src, "tags": tags, "weight": _weight(atype),
            "hub": primary, "_hubs": tags or (["agent"] if primary else []),
            "hash": content_hash(heading + body),
        }, edges, hubs_seen)


def _add_linked_preference(atoms: dict, edges: set, hubs_seen: set, *, src: str,
                           doc_id: str, aid: str, label: str, text: str, ts: int,
                           weight: float, tags: list[str] | None = None) -> None:
    """A `preference` atom on hub `agent`, linked `derived-from` its source doc."""
    _add_atom(atoms, {
        "id": aid, "label": label[:90], "type": "preference", "created": ts,
        "text": text[:2500], "source": src, "tags": tags or ["agent"], "weight": weight,
        "hub": "agent", "_hubs": ["agent"], "hash": content_hash(text),
    }, edges, hubs_seen)
    edges.add((aid, doc_id, "derived-from"))


def _short_label(body: str, limit: int = 80) -> str:
    """A short label for a persona bullet: its bold lead, else its first
    sentence, else the whole (cleaned) bullet."""
    text = _clean(body)
    m = re.match(r"\*\*(.+?)\*\*", body.strip())
    if m:
        return m.group(1).strip().rstrip(":").strip()[:limit]
    if len(text) <= limit:
        return text
    return (re.split(r"(?<=[.!?])\s+", text)[0].strip() or text)[:limit]


def _section_items(body: str) -> list[str]:
    """A section's bullets: its list items if it has any, else its paragraphs."""
    items = [ln.strip()[2:].strip() for ln in body.splitlines()
             if ln.strip().startswith(("- ", "* "))]
    if items:
        return [i for i in items if i]
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _persona_tags(heading: str) -> list[str]:
    if heading == "Core Truths":
        return ["agent", "soul", "core-truths"]
    if heading == "Boundaries":
        return ["agent", "soul", "boundaries"]
    return ["agent", "soul", "subagent", "delegation"]


def _parse_identity_fields(text: str, src: str, doc_id: str, ts: int,
                           atoms: dict, edges: set, hubs_seen: set) -> None:
    """IDENTITY.md's `- Name: Dale` fields → `identity` atoms on hub `agent`.

    Type `identity` (not `preference`) keeps persona facts distinct from the
    behavioural rules, and matches the type already used for identity-doc nodes.
    """
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(("- ", "* ")):
            continue
        m = _IDENTITY_FIELD_RE.match(s[2:].strip())
        if not m:
            continue
        field, value = m.group(1).strip(), m.group(2).strip()
        if field.lower() not in _IDENTITY_FIELDS:
            continue
        if not value or (value.startswith("(") and value.endswith(")")):
            continue  # empty or unfilled placeholder, e.g. "(pick something you like)"
        aid = "a_idf_" + content_hash(f"{src}:{field.lower()}")[:12]
        _add_atom(atoms, {
            "id": aid, "label": f"Identity — {field.lower()}: {value}"[:90],
            "type": "identity", "created": ts, "text": f"{field}: {value}",
            "source": src, "tags": ["agent", field.lower()], "weight": 0.95, "hub": "agent",
            "_hubs": ["agent"], "hash": content_hash(value),
        }, edges, hubs_seen)
        edges.add((aid, doc_id, "derived-from"))


def _parse_soul_sections(text: str, src: str, doc_id: str, ts: int,
                         atoms: dict, edges: set, hubs_seen: set) -> set[str]:
    """SOUL.md's `Core Truths` / `Boundaries` / rule-like `Subagent Delegation`
    bullets, each as one per-bullet `preference` atom.

    Returns the captured bullet bodies so the generic directive parser does not
    create a *second* atom for the same line (e.g. the `- Never …` Boundary)
    with a different label/tags.
    """
    captured: set[str] = set()
    for heading, body in sections(text):
        if heading not in _SOUL_SECTIONS and heading != "Subagent Delegation":
            continue
        for item in _section_items(body):
            if len(item) < 20:
                continue  # too short to be a rule or truth
            if heading == "Subagent Delegation" and not _RULE_HINT.search(item):
                continue  # bullets only when they read as rules
            tags, text = _pin_tags(_persona_tags(heading), item)
            aid = "p_" + content_hash(f"{src}:{text}")[:12]
            _add_linked_preference(
                atoms, edges, hubs_seen, src=src, doc_id=doc_id, aid=aid,
                label=f"{heading} — {_short_label(text)}", text=text, ts=ts,
                weight=0.85, tags=tags)
            captured.add(item)
    return captured


def _parse_identity(path: Path, atoms: dict, edges: set, hubs_seen: set) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    ts = int(path.stat().st_mtime * 1000)
    src = str(path.relative_to(config.WORKSPACE))
    doc_id = "a_id_" + content_hash(src)[:12]

    # Structured persona: IDENTITY.md fields and SOUL.md bullets, in addition
    # to the `Always/Never/Prefer/...` directive lines handled below.
    captured: set[str] = set()
    if path.name == "IDENTITY.md":
        _parse_identity_fields(text, src, doc_id, ts, atoms, edges, hubs_seen)
    elif path.name == "SOUL.md":
        captured = _parse_soul_sections(text, src, doc_id, ts, atoms, edges, hubs_seen)

    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(("- ", "* ")):
            continue
        body = s[2:].strip()
        if body in captured:
            continue  # already captured as a per-bullet persona preference
        if len(body) < 25 or body.startswith("["):
            continue
        if not re.match(r"(Always|Never|Prefer|Use|Keep|Avoid|Record|Store|Begin|Write|Update|Do not)", body, re.I):
            continue
        tags, text = _pin_tags(_hubs_for(body), body)
        aid = "p_" + content_hash(f"{src}:{text}")[:12]
        _add_atom(atoms, {
            "id": aid, "label": text[:80], "type": "preference", "created": ts,
            "text": text, "source": src, "tags": tags, "weight": 0.85,
            "hub": "agent", "_hubs": _hubs_for(body) or ["agent"], "hash": content_hash(text),
        }, edges, hubs_seen)
        edges.add((aid, doc_id, "derived-from"))


def import_mind(conn: sqlite3.Connection) -> dict:
    conn.executescript(SCHEMA)
    # PORT-NOTE: S4 — snapshot the rank store before the rebuild. The rebuild
    # deletes and re-creates the doc-derived atoms, which cascades their m_rank
    # rows; restoring the survivors' rows below keeps quality/counters/state
    # (e.g. a forgotten atom) across ingest runs.
    keep_rank = [
        tuple(r) for r in conn.execute(
            "SELECT atom_id, quality, served, interacted, last_served, state, state_since, updated "
            "FROM m_rank")
    ]
    # rebuild doc-derived atoms/edges; keep agent-remembered (id 'm_') and dream
    # (id 'd_') atoms — dreams live durably in the DB, the files are only the
    # transport and may be cleaned up as they grow
    conn.execute("DELETE FROM m_atoms WHERE id NOT LIKE 'm_%' AND id NOT LIKE 'd_%'")
    conn.execute("DELETE FROM m_edges WHERE source NOT LIKE 'm_%' AND source NOT LIKE 'd_%'")

    atoms: dict[str, dict] = {}
    edges: set[tuple[str, str, str]] = set()
    hubs_seen: set[str] = set()

    # hubs have no intrinsic birth; set when first mentioned
    docs = config.all_docs()
    for category, path in docs:
        if path.name == "DREAMS.md" or "dreaming/" in str(path) or ".dreams/" in str(path):
            _parse_markdown(path, "dream", atoms, edges, hubs_seen)
        elif category == "identity":
            _parse_identity(path, atoms, edges, hubs_seen)
            # the doc itself as a node, so its real content is inspectable
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(config.WORKSPACE))
            aid = "a_id_" + content_hash(rel)[:12]
            _add_atom(atoms, {
                "id": aid, "label": path.name, "type": "identity",
                "created": int(path.stat().st_mtime * 1000), "text": text[:6000],
                "source": rel, "tags": ["agent"], "weight": 0.9, "hub": "agent",
                "_hubs": ["agent"], "hash": content_hash(text),
            }, edges, hubs_seen)
        else:
            _parse_markdown(path, category, atoms, edges, hubs_seen)

    # --- derived-from: body mentions another atom's label ---
    items = list(atoms.values())
    by_label = [(a, a["label"].lower()) for a in items if len(a["label"]) > 8]
    for a in items:
        body = (a["text"] or "").lower()
        for other, lbl in by_label:
            if other["id"] == a["id"]:
                continue
            if lbl in body:
                edges.add((a["id"], other["id"], "derived-from"))
                if len(edges) > 4000:
                    break

    # --- consolidates (dream → memories) ---
    ordered = sorted(items, key=lambda x: x["created"])
    dreams = [a for a in ordered if a["type"] == "dream"]
    for d in dreams:
        for a in ordered:
            if a["type"] == "dream":
                continue
            if 0 <= d["created"] - a["created"] <= 36 * 3600_000:
                edges.add((d["id"], a["id"], "consolidates"))

    # hub birth = first mention
    hub_birth: dict[str, int] = {}
    for a in items:
        for h in a["_hubs"]:
            hub_birth[h] = min(hub_birth.get(h, 1 << 62), a["created"])

    cur = conn.cursor()
    for hid, (label, _kw) in HUBS.items():
        cur.execute("INSERT OR REPLACE INTO m_hubs(id,label,created) VALUES(?,?,?)",
                    (hid, label, hub_birth.get(hid, now_ms())))
    for a in items:
        cur.execute(
            """INSERT OR REPLACE INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (a["id"], a["label"], a["type"], a["created"], a["text"], a["source"],
             ",".join(a["tags"]), a["weight"], a["hub"], a["hash"]),
        )
    for s, t, k in edges:
        if s in atoms and (t in atoms or t in HUBS):
            cur.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (s, t, k))
    conn.commit()
    # PORT-NOTE: S4 — restore the rank rows for atoms that survived the rebuild
    # (matching their old ids); atoms gone with the rebuild simply stay absent.
    if keep_rank:
        live = {r[0] for r in conn.execute("SELECT id FROM m_atoms")}
        conn.executemany(
            """INSERT OR IGNORE INTO m_rank
                 (atom_id, quality, served, interacted, last_served, state, state_since, updated)
               VALUES (?,?,?,?,?,?,?,?)""",
            [r for r in keep_rank if r[0] in live],
        )
        conn.commit()
    _add_supersessions(conn)

    return {
        "atoms": len(atoms),
        "hubs": len(HUBS),
        "edges": sum(1 for _ in conn.execute("SELECT 1 FROM m_edges")),
        "by_type": {r[0]: r[1] for r in conn.execute("SELECT type, COUNT(*) FROM m_atoms GROUP BY type")},
    }


# --- queries -------------------------------------------------------------

def graph(conn: sqlite3.Connection) -> dict:
    nodes = []
    for r in conn.execute("SELECT id,label,type,created,text,source,tags,weight,hub FROM m_atoms"):
        nodes.append({
            "id": r[0], "label": r[1], "kind": "atom", "type": r[2], "created": r[3],
            "text": r[4], "source": r[5], "tags": [t for t in (r[6] or "").split(",") if t],
            "weight": r[7], "hub": r[8],
        })
    for r in conn.execute("SELECT id,label,created FROM m_hubs"):
        nodes.append({"id": r[0], "label": r[1], "kind": "hub", "type": r[0], "created": r[2]})
    links = [{"source": r[0], "target": r[1], "type": r[2]} for r in conn.execute("SELECT source,target,type FROM m_edges")]
    return {"nodes": nodes, "links": links}


def atom_detail(conn: sqlite3.Connection, atom_id: str) -> dict | None:
    r = conn.execute("SELECT id,label,type,created,text,source,tags,weight,hub FROM m_atoms WHERE id=?", (atom_id,)).fetchone()
    if not r:
        return None
    links = [{"source": a, "target": b, "type": t} for a, b, t in conn.execute(
        "SELECT source,target,type FROM m_edges WHERE source=? OR target=?", (atom_id, atom_id))]
    return {
        "id": r[0], "label": r[1], "type": r[2], "created": r[3], "text": r[4], "source": r[5],
        "tags": [t for t in (r[6] or "").split(",") if t], "weight": r[7], "hub": r[8], "links": links,
    }


def stats(conn: sqlite3.Connection) -> dict:
    out = {
        "atoms": conn.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0],
        "hubs": conn.execute("SELECT COUNT(*) FROM m_hubs").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM m_edges").fetchone()[0],
        "by_type": {r[0]: r[1] for r in conn.execute("SELECT type, COUNT(*) FROM m_atoms GROUP BY type")},
        "by_edge": {r[0]: r[1] for r in conn.execute("SELECT type, COUNT(*) FROM m_edges GROUP BY type")},
    }
    try:
        out["embeddings"] = conn.execute("SELECT COUNT(*) FROM m_embeddings").fetchone()[0]
        out["embed_model"] = embeddings.model_key()
        out["embed_dim"] = conn.execute(
            "SELECT MAX(dim) FROM m_embeddings WHERE model=?", (embeddings.model_key(),)
        ).fetchone()[0]
    except sqlite3.Error:
        out["embeddings"] = 0
    return out


def activity(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    return [
        {"ts": r[0], "op": r[1], "id": r[2], "label": r[3], "query": r[4]}
        for r in conn.execute("SELECT ts,op,atom_id,label,query FROM m_ops ORDER BY id DESC LIMIT ?", (limit,))
    ]


def _origin(aid: str, atype: str) -> str:
    """Where a memory came from — the Memory feed shows this as a badge."""
    if aid.startswith("m_pd_"):
        return "promoted"
    if aid.startswith("m_"):
        return "stored"
    if aid.startswith("a_id_") or atype == "identity":
        return "identity"
    if aid.startswith("d_"):
        return "dream"
    if aid.startswith("p_"):
        return "rule"
    if aid.startswith("a_"):
        return "note"
    return "memory"


def memories(conn: sqlite3.Connection, *, q: str | None = None,
             types: list[str] | None = None, hubs: list[str] | None = None,
             limit: int = 500, offset: int = 0) -> dict:
    """The Memory feed: every atom in the mind, newest first, filterable.

    Returns the page of items, the filtered `total`, and per-type `counts` for
    the filter chips. `q` is a case-insensitive substring over the text, label,
    tags, hub and source.
    """
    where: list[str] = []
    params: list = []
    if types:
        where.append("a.type IN (%s)" % ",".join("?" * len(types)))
        params.extend(types)
    if hubs:
        where.append("a.hub IN (%s)" % ",".join("?" * len(hubs)))
        params.extend(hubs)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append("(a.label LIKE ? OR a.text LIKE ? OR a.tags LIKE ? OR a.hub LIKE ? OR a.source LIKE ?)")
        params.extend([like] * 5)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM m_atoms a" + wsql, params).fetchone()[0]
    rows = conn.execute(
        "SELECT a.id,a.label,a.type,a.created,a.text,a.source,a.tags,a.weight,a.hub,a.session_id,"
        " r.quality,r.state,r.served,r.interacted,r.last_served,"
        " (SELECT COUNT(*) FROM m_feedback f WHERE f.atom_id=a.id AND f.signal='used') AS used,"
        " (SELECT COUNT(*) FROM m_feedback f WHERE f.atom_id=a.id AND f.signal='useful') AS useful,"
        " (SELECT COUNT(*) FROM m_feedback f WHERE f.atom_id=a.id AND f.signal='noise') AS noise "
        "FROM m_atoms a LEFT JOIN m_rank r ON r.atom_id=a.id" + wsql
        + " ORDER BY a.created DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    items = []
    for r in rows:
        used, useful, noise = r[15] or 0, r[16] or 0, r[17] or 0
        items.append({
            "id": r[0], "label": r[1], "type": r[2], "created": r[3], "text": r[4],
            "source": r[5], "tags": [t for t in (r[6] or "").split(",") if t],
            "weight": r[7], "hub": r[8], "origin": _origin(r[0], r[2]),
            # Provenance: the session the memory was saved from (may be NULL).
            "session_id": r[9],
            # Rank: quality/state and exposure, straight from m_rank. `ranked`
            # is False while the atom has no feedback signal at all — the UI
            # shows those as "unranked" (quality still sits at the 0.5 prior).
            "quality": r[10] if r[10] is not None else 0.5,
            "state": r[11] or "active",
            "served": r[12] or 0, "interacted": r[13] or 0, "last_served": r[14],
            "used": used, "useful": useful, "noise": noise,
            "ranked": (used + useful + noise) > 0,
        })
    counts = {r[0]: r[1] for r in conn.execute("SELECT type, COUNT(*) FROM m_atoms GROUP BY type")}
    return {"items": items, "total": total, "counts": counts}


# --- agent tools (M2) ---------------------------------------------------

LINK_TYPES = {"about", "supersedes", "caused-by", "derived-from", "consolidates"}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # PORT-NOTE: S4 — also run the additive migration so a legacy v1 store is
    # stamped/backfilled (m_rank) the first time any mind operation touches it.
    _schema_migrate(conn)


def _commit(conn: sqlite3.Connection, tries: int = 6) -> None:
    """Commit, retrying briefly if another writer (the ingest loop) holds the DB."""
    for attempt in range(tries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            time.sleep(0.25 * (attempt + 1))
    conn.commit()


def _ensure_hub(conn: sqlite3.Connection, hid: str, label: str | None = None) -> None:
    hid = _canon(hid)
    if not conn.execute("SELECT 1 FROM m_hubs WHERE id=?", (hid,)).fetchone():
        conn.execute("INSERT OR REPLACE INTO m_hubs(id,label,created) VALUES(?,?,?)",
                     (hid, label or HUBS.get(hid, (hid, []))[0], now_ms()))


def log_op(conn: sqlite3.Connection, op: str, atom_id: str | None = None,
           label: str | None = None, query: str | None = None,
           session_id: str | None = None) -> None:
    _ensure_schema(conn)
    try:
        conn.execute("INSERT INTO m_ops(ts,op,atom_id,label,query,session_id) VALUES(?,?,?,?,?,?)",
                     (now_ms(), op, atom_id, label, query, session_id))
        conn.commit()
    except sqlite3.OperationalError:
        # best effort: a concurrent writer must never break a memory operation
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def remember(conn: sqlite3.Connection, text: str, *, atype: str = "observation",
             hubs: list[str] | None = None, links: dict[str, list[str]] | None = None,
             source: str | None = None, session_id: str | None = None,
             label: str | None = None, weight: float | None = None,
             dedupe: bool = False) -> dict:
    """Store a memory atom, wire its edges, and log the store.

    ``dedupe=True`` makes an exact re-store idempotent: if an atom with the same
    content hash and source already exists, it is returned unchanged (no new
    atom, no store op). The nightly deep phase uses this so re-running it cannot
    duplicate a lesson it already stored. Matching is exact text, not semantic.
    """
    _ensure_schema(conn)
    now = now_ms()
    text = text.strip()
    if dedupe:
        row = conn.execute(
            "SELECT id,label,type,created FROM m_atoms WHERE hash=? AND source IS ? "
            "ORDER BY created LIMIT 1",
            (content_hash(text), source),
        ).fetchone()
        if row:
            existing = [h for (h,) in conn.execute(
                "SELECT target FROM m_edges WHERE source=? AND type='about'", (row[0],))]
            return {"id": row[0], "label": row[1], "type": row[2],
                    "hubs": existing, "created": row[3], "deduped": True}
    hubs = [_canon(h) for h in (hubs or _hubs_for(text)) if h] or ["agent"]
    primary = hubs[0]
    lbl = (label or " ".join(text.split()[:8]))[:90]
    aid = "m_" + content_hash(f"{now}:{text}")[:12]
    conn.execute(
        """INSERT OR REPLACE INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash,session_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, lbl, atype, now, text, source, ",".join(hubs),
         weight if weight is not None else _weight(atype), primary, content_hash(text), session_id))
    for h in hubs:
        _ensure_hub(conn, h)
        conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (aid, h, "about"))
    for kind, targets in (links or {}).items():
        if kind not in LINK_TYPES:
            continue
        for target in targets:
            conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (aid, target, kind))
    # PORT-NOTE: S4 — born with a rank row (active, quality 0.5) so exposure
    # counters and the forgetting state machine exist from the first serve.
    rank.ensure_row(conn, aid, now=now)
    _commit(conn)
    log_op(conn, "store", aid, lbl, None, session_id)
    # Best-effort: give the new atom a vector now so it is immediately
    # semantically reachable. Ollama being down must not fail the memory write.
    embeddings.embed_atom(conn, aid)
    return {"id": aid, "label": lbl, "type": atype, "hubs": hubs, "created": now}


def link(conn: sqlite3.Connection, source_id: str, target_id: str, kind: str,
         session_id: str | None = None) -> dict:
    _ensure_schema(conn)
    if kind not in LINK_TYPES:
        raise ValueError(f"unknown link type: {kind}")
    conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (source_id, target_id, kind))
    _commit(conn)
    log_op(conn, "link", source_id, target_id, kind, session_id)
    return {"source": source_id, "target": target_id, "type": kind}


def forget(conn: sqlite3.Connection, atom_id: str, session_id: str | None = None) -> dict:
    _ensure_schema(conn)
    conn.execute("DELETE FROM m_atoms WHERE id=?", (atom_id,))
    conn.execute("DELETE FROM m_edges WHERE source=? OR target=?", (atom_id, atom_id))
    try:
        embeddings.ensure_schema(conn)
        conn.execute("DELETE FROM m_embeddings WHERE atom_id=?", (atom_id,))
    except sqlite3.Error:
        pass
    _commit(conn)
    log_op(conn, "forget", atom_id, None, None, session_id)
    return {"forgotten": atom_id}


# --- memory feedback (quality signal) -----------------------------------
#
# Usage (`used`) is objective: the plugin records it when memory_open(id)
# succeeds. Rating (`useful` / `noise`) is subjective: the agent calls
# memory_rate(id, verdict). `unused` is inferred by the plugin: a memory the
# recall lane pushed but the agent neither opened nor rated this turn — a weak
# attention signal, not a verdict. All feed a *small* capped multiplier in
# recall ranking, so relevance still dominates (see _feedback_multiplier).

FEEDBACK_SIGNALS = ("used", "useful", "noise", "unused")


def feedback(conn: sqlite3.Connection, atom_id: str, signal: str, *,
             source: str | None = None, session_id: str | None = None) -> dict:
    """Record one memory quality signal. Best-effort: a concurrent writer must
    never turn a feedback ping into an error, so we retry briefly and, if the
    store stays locked, report `recorded: false` instead of raising."""
    _ensure_schema(conn)
    if signal not in FEEDBACK_SIGNALS:
        raise ValueError(f"unknown feedback signal: {signal}")
    # The memory's home session (where it was saved from), recorded alongside
    # the rating session so a verdict keeps both ends of the provenance chain.
    # Best-effort: an unknown atom simply leaves it NULL.
    row = conn.execute("SELECT session_id FROM m_atoms WHERE id=?", (atom_id,)).fetchone()
    origin_session_id = row[0] if row else None
    recorded = False
    for attempt in range(5):
        try:
            conn.execute(
                "INSERT INTO m_feedback(ts,atom_id,signal,source,session_id,origin_session_id) "
                "VALUES(?,?,?,?,?,?)",
                (now_ms(), atom_id, signal, source, session_id, origin_session_id),
            )
            _commit(conn)
            recorded = True
            break
        except sqlite3.OperationalError as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if "locked" not in str(exc).lower() or attempt == 4:
                break
            time.sleep(0.2 * (attempt + 1))
    if recorded:
        log_op(conn, "feedback", atom_id, signal, None, session_id)
        # PORT-NOTE: S4 — keep the deterministic rank store in step with the
        # feedback log: count the interaction, promote an ignored/archived atom
        # that just proved value (useful/used), and refresh its quality.
        # `unused` is skipped: it is high-volume and soft (the recall multiplier
        # reads it directly), and a per-atom rank recompute is a full-graph scan
        # that a per-turn inferred signal must not trigger.
        # Best-effort — rank bookkeeping must never turn a ping into an error.
        if signal != "unused":
            try:
                rank.on_feedback(conn, atom_id, signal)
            except sqlite3.Error:
                pass
    return {"atom_id": atom_id, "signal": signal, "source": source,
            "session_id": session_id, "origin_session_id": origin_session_id,
            "recorded": recorded}


def rating_volume(conn: sqlite3.Connection, *, days: int = 7,
                  at_ms: int | None = None) -> dict:
    """A rating-volume snapshot over the last ``days`` (the rating-watch metric).

    Counts the subjective verdicts (``useful``/``noise``), the objective ``used``
    nudges, the inferred ``unused`` signals, and the distinct sessions that
    rated, plus how many atoms still carry no subjective verdict at all
    ("unranked"). Read-only; safe under the single-writer daemon.
    """
    _ensure_schema(conn)
    at = at_ms if at_ms is not None else now_ms()
    since = at - max(1, days) * 86_400_000
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN signal='useful' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN signal='noise' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN signal='used' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN signal='unused' THEN 1 ELSE 0 END), "
        "COUNT(DISTINCT CASE WHEN signal IN ('useful','noise') THEN session_id END) "
        "FROM m_feedback WHERE ts >= ?",
        (since,),
    ).fetchone()
    useful, noise, used, unused, raters = (int(v or 0) for v in row)
    unranked = conn.execute(
        "SELECT COUNT(*) FROM m_atoms a WHERE NOT EXISTS ("
        "SELECT 1 FROM m_feedback f WHERE f.atom_id = a.id "
        "AND f.signal IN ('useful','noise'))"
    ).fetchone()[0]
    atoms = conn.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0]
    return {
        "days": max(1, days), "since_ms": since, "at_ms": at,
        "useful": useful, "noise": noise, "used": used, "unused": unused,
        "raters": raters, "rated": useful + noise,
        "unranked": int(unranked), "atoms": int(atoms),
    }


def _feedback_counts(conn: sqlite3.Connection) -> dict[str, tuple[int, int, int, int]]:
    """atom_id -> (used, useful, noise, unused) counts, from the feedback log."""
    counts: dict[str, list[int]] = {}
    for aid, sig, n in conn.execute(
        "SELECT atom_id, signal, COUNT(*) FROM m_feedback GROUP BY atom_id, signal"
    ):
        if not aid:
            continue
        c = counts.setdefault(aid, [0, 0, 0, 0])
        if sig == "used":
            c[0] = n
        elif sig == "useful":
            c[1] = n
        elif sig == "noise":
            c[2] = n
        elif sig == "unused":
            c[3] = n
    return {k: tuple(v) for k, v in counts.items()}


# --- recall ranking ------------------------------------------------------
#
# The old scorer summed raw substring counts over label+text+tags+hub, so a
# long or recent atom that happened to repeat a common query word ("what",
# "the") beat a short, precise one. The scorer below is a small, deterministic
# BM25-flavoured model:
#
#   score = [ Σ_t idf(t) · Σ_f w_f · sat(tf) ] / (1 + ln(1 + len))
#           × (hub-anchor bonus) × (mild weight factor)
#
# with idf(t) = ln(1 + (N - df + 0.5) / (df + 0.5)) and the BM25 term-frequency
# saturation sat(tf) = tf·(k1+1)/(tf+k1). A term rare in the corpus counts
# more; a hit in `label`/`tags`/`hub` counts more than one in `text`; repeated
# matches saturate so they cannot dominate; and dividing by the atom's length
# makes a match in a short atom win. The query's own hubs (via `_hubs_for`, so
# synonyms count: "Matter" → `ha`) anchor atoms whose hub matches.
# `weight`/`created` only break near-ties.

# Closed-class / boilerplate words that alone carry no retrieval signal. IDF
# already discounts what merely happens to be frequent; this list removes the
# small set of words that would otherwise match almost every atom.
_RECALL_STOP = frozenset("""
a an and are as at be been being but by can could did do does doing for from had has have
having he her here hers him his how i if in into is it its just me might more most much must
my no nor not of off on once only or other our out over own same shall she should so some such
than that the their them then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you your
apply yes
""".split())

# A hit in the label, the tags or the hub is far more meaningful than one
# buried in the body text.
_FIELD_WEIGHTS = (("label", 3.0), ("tags", 2.5), ("hub", 2.5), ("text", 1.0))
# A query-hub match (including synonyms) multiplies an atom's relevance.
_HUB_BOOST = 1.5
# BM25 term-frequency saturation constant.
_TF_K1 = 1.2

# Feedback is a *small* capped multiplier: relevance must still dominate. The
# net rating (useful - noise) supplies the sign and most of the budget; a tiny
# positive nudge comes from how often the atom was actually opened (`used`), and
# a tiny negative drag from how often it was pushed but never used (`unused`).
_FEEDBACK_CAP = 0.10        # max ±10% on the final score
_FEEDBACK_NET_SCALE = 3.0   # net ±3 saturates the rating budget
_FEEDBACK_USE_SCALE = 5.0   # 5 opens saturates the usage nudge
_FEEDBACK_USE_SHARE = 0.2   # usage is at most 20% of the feedback budget
_FEEDBACK_IDLE_SCALE = 5.0  # 5 unused serves saturate the idle drag
_FEEDBACK_IDLE_SHARE = 0.1  # the idle drag is at most 10% of the budget (half the usage nudge)


def _feedback_multiplier(used: int = 0, useful: int = 0, noise: int = 0,
                         unused: int = 0) -> float:
    """Deterministic score multiplier in [1 - CAP, 1 + CAP].

    `net = useful - noise` drives the direction (tanh-saturated); a small
    non-negative nudge rewards memories that are actually opened, and a small
    non-negative drag punishes ones the recall lane keeps pushing but the agent
    never uses. Everything is bounded, so no amount of feedback can override a
    real relevance gap, and one `useful` outweighs any pile of `unused`.
    """
    net = math.tanh((useful - noise) / _FEEDBACK_NET_SCALE)
    use = _FEEDBACK_USE_SHARE * math.tanh(used / _FEEDBACK_USE_SCALE)
    idle = _FEEDBACK_IDLE_SHARE * math.tanh(unused / _FEEDBACK_IDLE_SCALE)
    adj = max(-1.0, min(1.0, net + use - idle))
    return 1.0 + _FEEDBACK_CAP * adj


def _score_atoms(atoms: list[dict], query: str,
                 feedback: dict[str, tuple[int, int, int]] | None = None,
                 *,
                 query_vec: list[float] | None = None,
                 vectors: dict | None = None,
                 alpha: float | None = None) -> list[tuple[float, dict]]:
    """Rank atom dicts by relevance to `query` (pure, deterministic, no DB).

    Returns `(score, atom)` best-first. Each atom needs `label`, `text`,
    `tags`, `hub`, `created` and `weight` keys; `tags` may be a CSV string.
    `feedback` maps an atom id to `(used, useful, noise, unused)` counts and
    applies the small capped multiplier from `_feedback_multiplier` (default:
    none).

    When `query_vec` and `vectors` (atom id -> L2-normalised vector) are given,
    ranking is **hybrid**::

        score = lexical_norm + alpha * cosine_norm

    where `lexical_norm` divides the BM25-flavoured score by the best lexical
    score for this query, and `cosine_norm` min-max stretches the (clamped)
    cosine over the candidate set to `[0, 1]`. Both terms are therefore on the
    same 0..1 scale for *any* embedding model — the raw cosine of different
    providers lives on different baselines, so min-max is what makes `alpha`
    portable between them. Without vectors (or with a query that has no vector)
    the function is byte-for-byte the old lexical scorer.
    """
    terms = list(dict.fromkeys(
        t for t in re.findall(r"[a-z0-9]+", (query or "").lower())
        if len(t) > 2 and t not in _RECALL_STOP
    ))
    atoms = list(atoms)
    use_semantic = bool(query_vec is not None and vectors)
    if (not terms and not use_semantic) or not atoms:
        return []
    if alpha is None:
        alpha = config.RECALL_ALPHA

    prepared: list[tuple[dict, dict[str, Counter], int]] = []
    df: Counter = Counter()
    for a in atoms:
        fields: dict[str, Counter] = {}
        seen: set[str] = set()
        length = 0
        for name, _w in _FIELD_WEIGHTS:
            value = str(a.get(name) or "")
            length += len(value)
            toks = Counter(re.findall(r"[a-z0-9]+", value.lower()))
            fields[name] = toks
            seen.update(toks)
        for t in terms:
            if t in seen:
                df[t] += 1
        prepared.append((a, fields, length))

    n_docs = len(prepared)
    query_hubs = set(_hubs_for(query))

    def _lexical_core(fields: dict[str, Counter], length: int) -> float:
        raw = 0.0
        for t in terms:
            d = df.get(t, 0)
            if not d:
                continue
            idf = math.log(1.0 + (n_docs - d + 0.5) / (d + 0.5))
            tf_sum = 0.0
            for name, weight in _FIELD_WEIGHTS:
                tf = fields[name].get(t, 0)
                if tf:
                    tf_sum += weight * (tf * (_TF_K1 + 1.0) / (tf + _TF_K1))
            raw += idf * tf_sum
        if raw <= 0:
            return 0.0
        return raw / (1.0 + math.log(1.0 + length))

    scored: list[tuple[float, dict]] = []

    if not use_semantic:
        # --- lexical only (unchanged behaviour) ---
        for a, fields, length in prepared:
            score = _lexical_core(fields, length)
            if score <= 0:
                continue
            if query_hubs and (a.get("hub") or "") in query_hubs:
                score *= _HUB_BOOST
            # importance is a *minor* factor: ±5% across the 0..1 weight range
            score *= 0.95 + 0.10 * float(a.get("weight") or 0.0)
            # usage/rating feedback: another *minor*, capped (±10%) factor
            if feedback:
                counts = feedback.get(a.get("id") or "")
                if counts:
                    score *= _feedback_multiplier(*counts)
            scored.append((score, a))
        scored.sort(key=lambda x: (-x[0], -(x[1].get("created") or 0), x[1].get("id") or ""))
        return scored

    # --- hybrid: lexical + semantic ---
    # Hub anchoring belongs to the lexical core; weight and feedback are applied
    # once, to the combined score, so semantic-only hits get them too.
    lex_core: dict[str, float] = {}
    for a, fields, length in prepared:
        core = _lexical_core(fields, length)
        if core <= 0:
            continue
        if query_hubs and (a.get("hub") or "") in query_hubs:
            core *= _HUB_BOOST
        lex_core[a.get("id") or ""] = core
    max_lex = max(lex_core.values(), default=0.0)

    # Min-max normalise the cosine over the candidate set so the semantic term
    # lands on the same 0..1 scale as `lex_norm` for any embedding provider.
    cos_by_id: dict[str, float] = {}
    for a, _fields, _length in prepared:
        aid = a.get("id") or ""
        vec = vectors.get(aid) if vectors else None
        if vec is None:
            continue
        cos_by_id[aid] = max(0.0, embeddings.dot(query_vec, vec))
    lo = min(cos_by_id.values(), default=0.0)
    hi = max(cos_by_id.values(), default=0.0)
    span = (hi - lo) or 1.0

    for a, _fields, _length in prepared:
        aid = a.get("id") or ""
        core = lex_core.get(aid, 0.0)
        cos = cos_by_id.get(aid)
        if core <= 0 and cos is None:
            continue
        lex_norm = (core / max_lex) if max_lex > 0 else 0.0
        cos_norm = ((cos - lo) / span) if cos is not None else 0.0
        score = lex_norm + alpha * cos_norm
        if score <= 0:
            continue
        score *= 0.95 + 0.10 * float(a.get("weight") or 0.0)
        if feedback:
            counts = feedback.get(aid)
            if counts:
                score *= _feedback_multiplier(*counts)
        scored.append((score, a))

    scored.sort(key=lambda x: (-x[0], -(x[1].get("created") or 0), x[1].get("id") or ""))
    return scored


def recall(conn: sqlite3.Connection, query: str, *, limit: int = 8,
           session_id: str | None = None, semantic: bool = True,
           alpha: float | None = None) -> dict:
    """Return a compact, relevant subgraph for a query, and log the recall.

    Ranking is **hybrid** (lexical + local semantic cosine) when embeddings
    exist and Ollama answers; otherwise it falls back to the lexical scorer.
    The response shape is unchanged either way.
    """
    _ensure_schema(conn)
    # PORT-NOTE: S4 — atoms the rank engine forgot (state != 'active') are never
    # recall candidates. An atom with no rank row counts as active, so an empty
    # or all-active m_rank reproduces the pre-S4 candidate set byte-for-byte.
    rows = list(conn.execute(
        "SELECT a.id, a.label, a.type, a.created, a.text, a.source, a.tags, a.weight, a.hub "
        "FROM m_atoms a WHERE NOT EXISTS ("
        "  SELECT 1 FROM m_rank r WHERE r.atom_id = a.id AND r.state <> 'active')"))
    candidates = [
        {"id": r[0], "label": r[1], "type": r[2], "created": r[3], "text": r[4],
         "source": r[5], "tags": r[6] or "", "weight": r[7], "hub": r[8]}
        for r in rows
    ]
    vectors = None
    query_vec = None
    if semantic:
        try:
            vectors = embeddings.load_vectors(conn) or None
            if vectors:
                query_vec = embeddings.embed_query(query)
                if query_vec is None:
                    vectors = None  # Ollama down -> lexical-only, no crash
        except Exception:  # noqa: BLE001 - recall must never fail on embeddings
            vectors = None
            query_vec = None
    top = [a for _s, a in _score_atoms(
        candidates, query, feedback=_feedback_counts(conn),
        query_vec=query_vec, vectors=vectors, alpha=alpha)[:limit]]
    # PORT-NOTE: S4 — record the exposure for each atom actually served. This
    # runs after ranking, so it can never affect the result order.
    if top:
        rank.record_served(conn, [a["id"] for a in top])
    ids = {a["id"] for a in top}
    edges = [{"source": a, "target": b, "type": t}
             for a, b, t in conn.execute("SELECT source,target,type FROM m_edges")
             if a in ids or b in ids]
    atoms = [{"id": a["id"], "label": a["label"], "type": a["type"], "created": a["created"],
              "text": a["text"], "source": a["source"],
              "tags": [t for t in (a["tags"] or "").split(",") if t],
              "weight": a["weight"], "hub": a["hub"]}
             for a in top]
    top_id = top[0]["id"] if top else None
    top_label = top[0]["label"] if top else None
    log_op(conn, "recall", top_id, top_label, query, session_id)
    return {"query": query, "count": len(atoms), "atoms": atoms, "links": edges}


def directives(conn: sqlite3.Connection, limit: int = 8) -> dict:
    """Standing directives — pinned persona rules, re-injected on every model call.

    A directive is a `preference`/`identity` atom carrying the ``pin`` tag, set by
    a ``[pin]`` marker in USER.md/SOUL.md or by ``neobrain pin``. When nothing is
    pinned the lane falls back to the highest-weight preferences so it is useful
    out of the box. Deterministic order (weight, recency, id).
    """
    _ensure_schema(conn)
    sql = ("SELECT id,label,text,type FROM m_atoms WHERE {where} "
           "ORDER BY weight DESC, created DESC, id LIMIT ?")
    rows = conn.execute(
        sql.format(where="type IN ('preference','identity') AND "
                         "',' || replace(coalesce(tags,''),' ','') || ',' LIKE '%,pin,%'"),
        (limit,),
    ).fetchall()
    pinned = bool(rows)
    if not rows:
        rows = conn.execute(sql.format(where="type='preference'"), (limit,)).fetchall()
    atoms = [{"id": r[0], "label": r[1], "text": r[2], "type": r[3]} for r in rows]
    log_op(conn, "directives", None, f"directives x{len(atoms)}", None, None)
    return {"pinned": pinned, "count": len(atoms), "atoms": atoms}


def set_pin(conn: sqlite3.Connection, atom_id: str, pinned: bool = True,
            session_id: str | None = None) -> dict:
    """Add/remove the ``pin`` tag on an atom (the standing-directive flag)."""
    _ensure_schema(conn)
    row = conn.execute("SELECT tags, label FROM m_atoms WHERE id=?", (atom_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown atom: {atom_id}")
    tags = [t for t in (row[0] or "").split(",") if t]
    had = "pin" in tags
    if pinned and not had:
        tags.append("pin")
    elif not pinned and had:
        tags.remove("pin")
    conn.execute("UPDATE m_atoms SET tags=? WHERE id=?", (",".join(tags), atom_id))
    _commit(conn)
    log_op(conn, "pin" if pinned else "unpin", atom_id, row[1], None, session_id)
    return {"id": atom_id, "label": row[1], "pinned": pinned, "changed": had != pinned}


def wake_up(conn: sqlite3.Connection, session_id: str | None = None) -> dict:
    """Compact identity + active threads + most-connected hubs."""
    _ensure_schema(conn)
    prefs = [dict(zip(("id", "label", "text"), r)) for r in conn.execute(
        "SELECT id,label,text FROM m_atoms WHERE type='preference' ORDER BY weight DESC LIMIT 8")]
    opens = [dict(zip(("id", "label", "text"), r)) for r in conn.execute(
        "SELECT id,label,text FROM m_atoms WHERE type='open' ORDER BY created DESC LIMIT 6")]
    recent = [dict(zip(("id", "label", "type", "hub"), r)) for r in conn.execute(
        "SELECT id,label,type,hub FROM m_atoms ORDER BY created DESC LIMIT 12")]
    hubs = [{"id": r[0], "label": r[1], "atoms": r[2]} for r in conn.execute(
        """SELECT h.id, h.label, COUNT(e.source) c FROM m_hubs h
           LEFT JOIN m_edges e ON e.target=h.id AND e.type='about'
           GROUP BY h.id ORDER BY c DESC LIMIT 10""")]
    log_op(conn, "recall", None, "wake-up pack", "wake up", session_id)
    return {"identity": prefs, "open_loops": opens, "recent": recent, "hubs": hubs}


# --- consolidation ("dream") pass ---------------------------------------

_STOP = set("""the a an and or of to in on for with without into from by is are was were be been being
this that these those it its as at we you i they he she them his her our your not no yes but if then
than so such can could would should will shall may might must do does did done have has had about
when where which who whom what how why all any both each few more most other some only own same too
very also just now new old use used using over under between after before during since until while""".split())

_STOP |= set(
    """service system config device devices failed failing first verified verify broken route routes
block blocked audio detect detection streams stream inventory running image make made works work needs
need still added created updated tests test error errors issue issues module modules""".split()
)
# hub ids and labels are never "salient themes"
_STOP |= set(HUBS) | {v[0].lower() for v in HUBS.values()}
_STOP |= set(ALIASES)


def _normalize_hubs(conn: sqlite3.Connection) -> int:
    """Merge aliased hub ids into their canonical hub across atoms and edges."""
    changed = 0
    for (hid,) in list(conn.execute("SELECT id FROM m_hubs")):
        c = _canon(hid)
        if c == hid:
            continue
        conn.execute("INSERT OR IGNORE INTO m_hubs(id,label,created) VALUES(?,?,?)",
                     (c, HUBS.get(c, (c.title(), []))[0], now_ms()))
        conn.execute("UPDATE m_atoms SET hub=? WHERE hub=?", (c, hid))
        for col in ("source", "target"):
            try:
                conn.execute(f"UPDATE m_edges SET {col}=? WHERE {col}=?", (c, hid))
            except sqlite3.IntegrityError:
                conn.execute(f"DELETE FROM m_edges WHERE {col}=?", (hid,))
        conn.execute("DELETE FROM m_hubs WHERE id=?", (hid,))
        changed += 1
    # agents may have stored non-canonical hub ids directly
    for (hid,) in list(conn.execute("SELECT DISTINCT hub FROM m_atoms WHERE hub IS NOT NULL")):
        c = _canon(hid)
        if c != hid:
            _ensure_hub(conn, c)
            conn.execute("UPDATE m_atoms SET hub=? WHERE hub=?", (c, hid))
            changed += 1
    _commit(conn)
    return changed


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9-]{5,}", (text or "").lower())
        if t not in _STOP and not re.fullmatch(r"[\d-]+", t)
    }


# --- day-note promotion (dream pass) ------------------------------------
#
# The daily note is where durable outcomes first land, but the doc importer
# types many of them `observation` — which is *not* auto-injected and produces
# no store event, so real work can sit in the mind unusable. The dream pass
# promotes each day-note section that reads as a durable decision/lesson into a
# curated `m_` atom (id `m_pd_*`) with a `derived-from` edge back to the doc
# atom.
#
# Sections the importer has *already* typed `decision`/`lesson` are recognised
# by `_promotion_kind` but deliberately skipped here: those doc atoms are
# already curated and injectable, so a promoted copy would only duplicate an
# already-usable memory (the kill-criterion failure). Promotion therefore
# targets the misclassification gap.
#
# The pass is deterministic and idempotent: ids are `m_pd_<hash(source:heading)>`
# so re-running never re-promotes, the set is *refreshed* each pass (a promoted
# atom whose section stopped reading as durable is dropped, an edited section is
# re-synced) and only our own `m_pd_*` atoms are ever touched.

PROMOTE_CAP = 20  # at most this many *new* promotions per pass (recent first)

_PROMOTE_LESSON = re.compile(
    r"\b(lesson|root\s*cause|learned|post-?mortem|retro(?:spective)?)\b", re.I)
_PROMOTE_LESSON_BODY = re.compile(
    r"(root\s*cause(?:\s*\([^)]*\))?\s*(?:is|was|:|—|-)|lesson\s+learned|the\s+lesson|takeaway)",
    re.I)
_PROMOTE_HEADING = re.compile(
    r"\b(fix(?:ed|es)?|resolved|implement(?:ed|ation)?|built|shipped|deployed|added|"
    r"removed|created|migrat\w*|refactor\w*|consolidat\w*|unified|merged|applied|"
    r"agreed|decided|chose|chosen|rebuilt|rework\w*|generalised|introduced|revived|"
    r"authoritative|upgraded|established|rejected|rolled?\s*back|revert(?:ed)?|"
    r"stage\s*\d|m\d+\s*done|milestone|adoption)\b",
    re.I,
)
_PROMOTE_BODY = re.compile(
    r"(the\s+fix\s+(?:is|was)|"
    r"\b(?:decided|chose|agreed|approved|reverted|superseded|shipped|deployed|implemented)\b)",
    re.I,
)
# headings that describe a symptom / raw notes / a question, never a durable outcome
_PROMOTE_DENY = re.compile(
    r"^\s*(symptom|notes?|docs?|access notes|open items?|still open|not yet|monitoring bug|"
    r"topology|git note|references?|related)\b",
    re.I,
)


def _promotion_kind(atype: str, heading: str, body: str) -> str | None:
    """Classify a day-note section as `decision`/`lesson`, or None.

    A section is durable when it is already typed decision/lesson, or it is an
    observation whose heading or body carries clear decision/lesson language
    (e.g. "Timeline — Stage 4: …", "— FIXED", "Root cause: …"). Headings that
    merely describe a symptom, notes or a question are denied.
    """
    if atype in ("decision", "lesson"):
        return atype
    if atype != "observation":
        return None
    h, b = heading or "", body or ""
    if _PROMOTE_DENY.match(h):
        return None
    if _PROMOTE_LESSON.search(h) or _PROMOTE_LESSON_BODY.search(b):
        return "lesson"
    if _PROMOTE_HEADING.search(h) or _PROMOTE_BODY.search(b):
        return "decision"
    return None


def _daily_note_atoms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Doc-derived atoms from `memory/YYYY-MM-DD.md`, newest section first.

    Within a day every section shares the note's `created`; `rowid` (insertion
    order = file order) breaks the tie so later sections rank first.
    """
    return list(conn.execute(
        "SELECT rowid,id,label,type,text,hub,tags,created,source FROM m_atoms "
        "WHERE id GLOB 'a_*' AND source GLOB "
        "'memory/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md' "
        "ORDER BY created DESC, source DESC, rowid DESC"
    ))


def _promote_daily_notes(conn: sqlite3.Connection, cap: int = PROMOTE_CAP,
                         *, embed: bool = True) -> dict:
    """Promote the day-note sections that read as durable decisions/lessons.

    Returns `{"promoted": ids_added, "refreshed": n_updated, "dropped": n_stale,
    "total": n_ours}`. Idempotent: already-promoted ids are skipped, so a second
    pass over unchanged notes adds nothing.
    """
    # Desired set = every day-note section that reads as a durable outcome and
    # is not already curated. Curated doc atoms (decision/lesson) are skipped to
    # avoid duplicating already-injectable memories (kill criterion).
    desired: list[dict] = []
    for r in _daily_note_atoms(conn):
        kind = _promotion_kind(r["type"], r["label"], r["text"])
        if not kind or r["type"] in ("decision", "lesson"):
            continue
        body = _clean(r["text"] or "")
        if len(body) < 40:
            continue
        desired.append({
            "id": "m_pd_" + content_hash(f"{r['source']}:{r['label']}")[:12],
            "label": (r["label"] or "")[:90],
            "type": kind,
            "created": r["created"],
            "text": body[:2500],
            "source": r["source"],
            "tags": r["tags"] or "",
            "hub": r["hub"] or "agent",
            "doc": r["id"],
        })

    existing = {
        r[0]: r[1] for r in conn.execute(
            "SELECT id, created FROM m_atoms WHERE id GLOB 'm_pd_*'")
    }
    desired_ids = {d["id"] for d in desired}

    # Refresh: drop promoted atoms whose section no longer reads as durable.
    dropped = 0
    for aid in list(existing):
        if aid in desired_ids:
            continue
        conn.execute("DELETE FROM m_atoms WHERE id=?", (aid,))
        conn.execute("DELETE FROM m_edges WHERE source=? OR target=?", (aid, aid))
        try:
            embeddings.ensure_schema(conn)
            conn.execute("DELETE FROM m_embeddings WHERE atom_id=?", (aid,))
        except sqlite3.Error:
            pass
        dropped += 1

    # Cap *new* promotions per pass, newest first (the recent day's outcomes).
    new_ids = [d["id"] for d in desired if d["id"] not in existing]
    to_add = set(new_ids[:max(0, cap)])
    added: list[str] = []
    refreshed = 0
    for d in desired:
        if d["id"] in existing:
            # re-sync the promoted atom to the current section; keep its birth
            old = conn.execute(
                "SELECT label,type,text,hub,tags FROM m_atoms WHERE id=?", (d["id"],)
            ).fetchone()
            if old and (old[0], old[1], old[2], old[3], old[4]) != (
                    d["label"], d["type"], d["text"], d["hub"], d["tags"]):
                conn.execute(
                    "UPDATE m_atoms SET label=?,type=?,text=?,hub=?,tags=?,weight=?,hash=? WHERE id=?",
                    (d["label"], d["type"], d["text"], d["hub"], d["tags"],
                     _weight(d["type"]), content_hash(d["label"] + d["text"]), d["id"]))
                refreshed += 1
        elif d["id"] in to_add:
            conn.execute(
                """INSERT OR REPLACE INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (d["id"], d["label"], d["type"], d["created"], d["text"], d["source"],
                 d["tags"], _weight(d["type"]), d["hub"], content_hash(d["label"] + d["text"])))
            added.append(d["id"])
        else:
            continue  # beyond this pass's cap
        # (re)wire provenance + hub for every atom we own
        conn.execute("DELETE FROM m_edges WHERE source=?", (d["id"],))
        for h in [t for t in (d["tags"] or "").split(",") if t] or [d["hub"]]:
            _ensure_hub(conn, _canon(h))
            conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)",
                         (d["id"], _canon(h), "about"))
        conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)",
                     (d["id"], d["doc"], "derived-from"))
    _commit(conn)

    # Best-effort, non-blocking: give the new/edited promoted atoms a vector now
    # so hybrid recall covers them immediately; the ingest loop retries anyway.
    embeddable = added + [d["id"] for d in desired if d["id"] in existing]
    if embed and getattr(config, "EMBED_ENABLED", True) and embeddable:
        try:
            embeddings.embed_ids(conn, embeddable)
        except Exception:  # noqa: BLE001 - embeddings must never break the dream pass
            pass

    # Log each new promotion as a `store` op so the item is visible live in the
    # observatory (the original gap: a day-note outcome produced no store event).
    labels = {d["id"]: d["label"] for d in desired}
    for pid in added:
        log_op(conn, "store", pid, labels.get(pid), "dream:promote")

    total = conn.execute("SELECT COUNT(*) FROM m_atoms WHERE id GLOB 'm_pd_*'").fetchone()[0]
    return {"promoted": added, "added": len(added), "refreshed": refreshed,
            "dropped": dropped, "total": total}


def consolidate(conn: sqlite3.Connection, session_id: str | None = None, _tries: int = 6) -> dict:
    """Retry wrapper: consolidation is idempotent, so a concurrent writer is fine."""
    for attempt in range(_tries):
        try:
            return _consolidate_inner(conn, session_id)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == _tries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _consolidate_inner(conn: sqlite3.Connection, session_id: str | None = None) -> dict:
    """Dream pass: normalise hubs, promote recurring observations to lessons,
    resolve conflicting decisions, and write a dream node linking what it did."""
    _ensure_schema(conn)
    from collections import Counter, defaultdict

    # regenerate from scratch so the pass is idempotent and tunable
    conn.execute("DELETE FROM m_edges WHERE source IN (SELECT id FROM m_atoms WHERE source='dream:consolidate')")
    conn.execute("DELETE FROM m_atoms WHERE source='dream:consolidate'")
    _commit(conn)

    merged = _normalize_hubs(conn)

    # 1) promote: ≥4 observations on a hub sharing a specific token → a lesson
    by_hub: dict[str, list] = defaultdict(list)
    for r in conn.execute("SELECT id,label,text,hub FROM m_atoms WHERE type='observation' AND hub IS NOT NULL"):
        by_hub[r[3]].append(r)
    promoted: list[str] = []
    for hub, rows in by_hub.items():
        freq: Counter = Counter()
        per: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            for t in _tokens(f"{r[1]} {r[2]}"):
                if len(t) >= 6:
                    freq[t] += 1
                    per[t].append(r[0])
        made = 0
        for tok, n in freq.most_common():
            if made >= 3:
                break
            if n < 4:
                continue
            lid = "m_p_" + content_hash(f"{hub}:{tok}")[:12]
            if conn.execute("SELECT 1 FROM m_atoms WHERE id=?", (lid,)).fetchone():
                continue
            text = f'Recurring theme "{tok}" across {n} observations on {hub}. Promoted by the dream pass.'
            conn.execute(
                """INSERT OR REPLACE INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (lid, f"Pattern: {tok} ({hub})"[:90], "lesson", now_ms(), text,
                 "dream:consolidate", hub, 0.7, hub, content_hash(text)))
            for aid in per[tok][:6]:
                conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (lid, aid, "derived-from"))
            promoted.append(lid)
            made += 1

    # 2) promote the day's durable note sections (the misclassification gap:
    #    decisions/lessons the importer typed `observation` → curated `m_` atoms)
    notes = _promote_daily_notes(conn, PROMOTE_CAP)

    # 3) supersede: a later memory that reverses an earlier one on the same hub
    superseded = _add_supersessions(conn)

    # 4) the dream node — a memory of the consolidation itself
    day = datetime.now().strftime("%Y-%m-%d")
    did = "m_d_" + content_hash("dream:" + day)[:12]
    summary = (
        f"Dream: consolidated the mind — merged {merged} hub alias(es), "
        f"promoted {len(promoted)} pattern(s) and {notes['added']} day-note section(s), "
        f"resolved {superseded} supersession(s)."
    )
    conn.execute(
        """INSERT OR REPLACE INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (did, f"Dream — {day}", "dream", now_ms(), summary, "dream:consolidate", "agent", 0.65, "agent", content_hash(summary)))
    for lid in promoted + notes["promoted"]:
        conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (did, lid, "consolidates"))
    _commit(conn)
    log_op(conn, "dream", did, "consolidation", summary, session_id)
    return {"merged_hubs": merged, "promoted": len(promoted), "notes_promoted": notes["added"],
            "notes_total": notes["total"], "notes_refreshed": notes["refreshed"],
            "notes_dropped": notes["dropped"], "superseded": superseded, "dream": did}


def _add_supersessions(conn: sqlite3.Connection) -> int:
    """Add supersedes edges where a later memory reverses an earlier one on the
    same hub, requiring a *specific* shared token (low document frequency) to
    avoid false pairs. Idempotent."""
    from collections import Counter

    raw = list(conn.execute("SELECT id,text,hub,created,type,label,source FROM m_atoms WHERE hub IS NOT NULL ORDER BY created"))
    # only reversal-bearing memories, and skip doc-section headings ("3. Foo")
    rows = [
        r for r in raw
        if r[4] in ("decision", "lesson", "open", "observation")
        and not re.match(r"^\s*\d+(\.\d+)*[\s.]", r[5] or "")
    ]
    toks = {r[0]: _tokens(r[1]) for r in rows}
    df: Counter = Counter()
    for r in rows:
        for t in toks[r[0]]:
            if len(t) >= 6:
                df[t] += 1
    # A `derived-from` pair is provenance, not replacement: a promoted memory
    # must never "supersede" the very doc atom it was promoted from. Mark both
    # directions and prune any such edge a previous pass may have written.
    derived = set()
    for x, y in conn.execute("SELECT source,target FROM m_edges WHERE type='derived-from'"):
        derived.add((x, y))
        derived.add((y, x))
    for x, y in list(conn.execute("SELECT source,target FROM m_edges WHERE type='supersedes'")):
        if (x, y) in derived:
            conn.execute("DELETE FROM m_edges WHERE source=? AND target=? AND type='supersedes'", (x, y))
    existing = {(a, b) for a, b in conn.execute("SELECT source,target FROM m_edges WHERE type='supersedes'")}
    added = 0
    for i, a in enumerate(rows):
        if a[4] not in ("decision", "lesson", "open"):
            continue
        if not re.search(r"(revert\w*|rollback\w*|supersede\w*|instead of|no longer)", a[1] or "", re.I):
            continue
        for b in reversed(rows[:i]):
            if b[2] != a[2]:
                continue
            if (a[0], b[0]) in derived:
                continue
            spec = [t for t in (toks[a[0]] & toks[b[0]]) if len(t) >= 6 and df[t] <= 5]
            # require real specificity: two narrow tokens, or one genuinely rare one
            if not (len(spec) >= 2 or any(df[t] <= 2 for t in spec)):
                continue
            if (a[0], b[0]) not in existing:
                conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (a[0], b[0], "supersedes"))
                existing.add((a[0], b[0]))
                added += 1
            break
    if added:
        _commit(conn)
    return added


def _clean_dream(text: str | None) -> str | None:
    """Strip OpenClaw bookkeeping markers and trailing deep-sleep blocks."""
    if not text:
        return text
    t = re.split(r"<!--\s*openclaw: dreaming: diary: end|<!--\s*openclaw:dreaming:diary:end|##\s*Deep Sleep", text)[0]
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"##\s*Deep Sleep.*", "", t, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def dreams(conn: sqlite3.Connection, limit_days: int = 30) -> dict:
    """Group each night's dream output: light/rem/deep files, durable lessons,
    promoted patterns and the consolidation node."""
    from collections import defaultdict

    g: dict[str, dict] = defaultdict(lambda: {
        "light": None, "rem": None, "deep": None, "narrative": None,
        "lessons": [], "promoted": [], "node": None,
    })
    for r in conn.execute("SELECT id,label,type,created,text,source,hub FROM m_atoms"):
        _id, label, atype, created, text, src, hub = r
        src = src or ""
        d = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d")
        if src.startswith("memory/dreaming/"):
            # The file's own date is authoritative: phases written near local
            # midnight get an atom timestamp on the previous UTC day (the
            # container runs UTC), which would mis-group them in the Dreams tab.
            m = re.search(r"(\d{4}-\d{2}-\d{2})", src)
            if m:
                d = m.group(1)
            parts = src.split("/")
            kind = parts[2] if len(parts) > 2 else ""
            if atype == "dream" and kind in ("light", "rem", "deep"):
                g[d][kind] = {"id": _id, "text": text}
            elif atype == "lesson":
                g[d]["lessons"].append({"id": _id, "label": label, "hub": hub})
        elif src == "DREAMS.md":
            g[d]["narrative"] = {"id": _id, "text": text, "label": label}
        elif src == "dream:consolidate":
            if atype == "lesson":
                g[d]["promoted"].append({"id": _id, "label": label, "hub": hub})
            elif atype == "dream":
                g[d]["node"] = {"id": _id, "text": text}

    out = []
    for d, v in sorted(g.items(), reverse=True):
        if not any([v["light"], v["rem"], v["deep"], v["narrative"], v["node"], v["lessons"]]):
            continue
        out.append({
            "date": d,
            "narrative": _clean_dream((v["narrative"] or v["rem"] or {}).get("text")),
            "narrative_source": "DREAMS.md" if v["narrative"] else ("rem" if v["rem"] else None),
            "light": _clean_dream((v["light"] or {}).get("text")),
            "deep": _clean_dream((v["deep"] or {}).get("text")),
            "nights_files": {"rem": bool(v["rem"]), "light": bool(v["light"]), "deep": bool(v["deep"])},
            "lessons": v["lessons"],
            "promoted": v["promoted"],
            "node_id": (v["node"] or {}).get("id"),
            "node_text": (v["node"] or {}).get("text"),
        })
        if len(out) >= limit_days:
            break
    return {"count": len(out), "dreams": out}


def reader(conn: sqlite3.Connection, group: str) -> dict:
    """Return the latest content of the docs in a group (memory / identity /
    infra / dream) for the Observatory's reader tabs."""
    if group not in ("memory", "identity", "infra", "dream"):
        raise ValueError(f"unknown group: {group}")
    items = []
    for p in config.doc_groups().get(group, []):
        try:
            rel = str(p.relative_to(config.WORKSPACE))
        except ValueError:
            rel = p.name
        row = conn.execute(
            "SELECT content, ts FROM doc_versions WHERE doc_path = ? ORDER BY ts DESC LIMIT 1",
            (str(p),),
        ).fetchone()
        content = row[0] if row and row[0] is not None else None
        if content is None:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
        ts = row[1] if row else int(p.stat().st_mtime * 1000)
        atom = conn.execute(
            "SELECT id FROM m_atoms WHERE source = ? ORDER BY created DESC LIMIT 1", (rel,)
        ).fetchone()
        items.append({"name": p.name, "path": rel, "updated": ts, "content": content,
                      "atom_id": atom[0] if atom else None})
    items.sort(key=lambda x: x["name"], reverse=(group == "memory"))
    return {"group": group, "count": len(items), "items": items}
