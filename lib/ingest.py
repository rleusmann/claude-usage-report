#!/usr/bin/env python3
"""Incrementally ingests Claude Code session transcripts into a compact SQLite database."""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    effort TEXT,
    speed TEXT,
    project TEXT,
    git_branch TEXT,
    input_tokens INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    thinking_tokens INTEGER NOT NULL,
    web_search INTEGER NOT NULL,
    web_fetch INTEGER NOT NULL,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id, ts);
CREATE TABLE IF NOT EXISTS tool_uses (
    tool_use_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    tool TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_uses_request ON tool_uses(request_id);
CREATE TABLE IF NOT EXISTS prompts (
    uuid TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    project TEXT,
    excerpt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prompts_session ON prompts(session_id, ts);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    title TEXT,
    custom_name TEXT,
    first_ts REAL,
    last_ts REAL,
    entrypoint TEXT,
    version TEXT,
    cost_state_usd REAL,
    lines_added INTEGER,
    lines_removed INTEGER,
    duration_ms INTEGER,
    api_duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS prs (
    url TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    number INTEGER,
    repository TEXT,
    ts REAL
);
CREATE TABLE IF NOT EXISTS subagents (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_type TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS limits (
    ts REAL PRIMARY KEY,
    five_pct REAL,
    five_reset REAL,
    week_pct REAL,
    week_reset REAL
);
CREATE TABLE IF NOT EXISTS history_daily (
    date TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    PRIMARY KEY (date, model)
);
"""

EXCERPT_LEN = 120

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
]
KEYWORD_VALUE = re.compile(
    r"(?i)\b(password|passwort|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|bearer|pre-?auth-?key)"
    r"(\"?\s*[:=]\s*\"?|\s+)([^\s\"',;]+)"
)
LONG_RANDOM = re.compile(r"\b(?=[A-Za-z0-9+_\-]*\d)(?=[A-Za-z0-9+_\-]*[A-Za-z])[A-Za-z0-9+_\-]{32,}={0,2}")
MASK = "•••"


def mask_secrets(text):
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    text = KEYWORD_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    return LONG_RANDOM.sub(MASK, text)


def make_excerpt(text):
    text = " ".join(mask_secrets(text).split())
    if len(text) > EXCERPT_LEN:
        text = text[: EXCERPT_LEN - 1].rstrip() + "…"
    return text


def human_prompt_text(d):
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    origin = d.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind not in (None, "human"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip() and any(b.get("type") == "image" for b in blocks):
            text = "[image]"
    else:
        return None
    text = text.strip()
    if not text:
        return None
    command = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if command:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return (command.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
    if text.startswith("<"):
        return None
    return text


def parse_ts(value):
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Pricing:
    def __init__(self, path):
        data = json.loads(Path(path).read_text())
        self.models = sorted(data["models"], key=lambda m: len(m["prefix"]), reverse=True)
        self.web_search = data.get("web_search_per_request", 0.0)
        self.fast = data.get("fast_mode_multiplier", 1.0)

    def lookup(self, model):
        for entry in self.models:
            if model.startswith(entry["prefix"]):
                return entry
        return None

    def cost(self, row):
        price = self.lookup(row["model"])
        if price is None:
            return None
        mult = self.fast if row.get("speed") == "fast" else 1.0
        tokens = (
            row["input_tokens"] * price["input"]
            + row["cache_write_5m"] * price["cache_write_5m"]
            + row["cache_write_1h"] * price["cache_write_1h"]
            + row["cache_read"] * price["cache_read"]
            + row["output_tokens"] * price["output"]
        )
        return tokens * mult / 1_000_000 + row["web_search"] * self.web_search


def usage_row(d, pricing):
    message = d.get("message") or {}
    usage = message.get("usage")
    model = message.get("model")
    request_id = d.get("requestId") or message.get("id")
    if not usage or not model or model.startswith("<") or not request_id:
        return None
    creation = usage.get("cache_creation") or {}
    write_total = usage.get("cache_creation_input_tokens") or 0
    write_5m = creation.get("ephemeral_5m_input_tokens")
    write_1h = creation.get("ephemeral_1h_input_tokens")
    if write_5m is None and write_1h is None:
        write_5m, write_1h = write_total, 0
    server = usage.get("server_tool_use") or {}
    row = {
        "request_id": request_id,
        "session_id": d.get("sessionId"),
        "agent_id": d.get("agentId") if d.get("isSidechain") else None,
        "ts": parse_ts(d.get("timestamp")),
        "model": model,
        "effort": d.get("effort"),
        "speed": usage.get("speed"),
        "project": d.get("cwd"),
        "git_branch": d.get("gitBranch"),
        "input_tokens": usage.get("input_tokens") or 0,
        "cache_write_5m": write_5m or 0,
        "cache_write_1h": write_1h or 0,
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
        "web_search": server.get("web_search_requests") or 0,
        "web_fetch": server.get("web_fetch_requests") or 0,
    }
    row["cost_usd"] = pricing.cost(row)
    return row


REQUEST_COLUMNS = [
    "request_id", "session_id", "agent_id", "ts", "model", "effort", "speed", "project", "git_branch",
    "input_tokens", "cache_write_5m", "cache_write_1h", "cache_read", "output_tokens", "thinking_tokens",
    "web_search", "web_fetch", "cost_usd",
]
UPSERT_REQUEST = (
    f"INSERT INTO requests ({', '.join(REQUEST_COLUMNS)}) VALUES ({', '.join('?' * len(REQUEST_COLUMNS))}) "
    "ON CONFLICT(request_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in REQUEST_COLUMNS[4:])
    + " WHERE excluded.output_tokens >= requests.output_tokens"
)


def touch_session(db, d):
    session_id = d.get("sessionId")
    ts = parse_ts(d.get("timestamp"))
    if not session_id or ts is None or d.get("isSidechain"):
        return
    db.execute(
        "INSERT INTO sessions (session_id, project, first_ts, last_ts, entrypoint, version) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "first_ts = MIN(COALESCE(sessions.first_ts, excluded.first_ts), excluded.first_ts), "
        "last_ts = MAX(COALESCE(sessions.last_ts, excluded.last_ts), excluded.last_ts), "
        "project = COALESCE(sessions.project, excluded.project), "
        "entrypoint = COALESCE(excluded.entrypoint, sessions.entrypoint), "
        "version = COALESCE(excluded.version, sessions.version)",
        (session_id, d.get("cwd"), ts, ts, d.get("entrypoint"), d.get("version")),
    )


def ensure_session(db, session_id):
    db.execute("INSERT OR IGNORE INTO sessions (session_id) VALUES (?)", (session_id,))


def handle_line(db, d, pricing):
    kind = d.get("type")
    if kind == "assistant":
        row = usage_row(d, pricing)
        if row is None or row["session_id"] is None or row["ts"] is None:
            return
        db.execute(UPSERT_REQUEST, [row[c] for c in REQUEST_COLUMNS])
        for block in (d.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use") and block.get("id"):
                db.execute(
                    "INSERT OR IGNORE INTO tool_uses (tool_use_id, request_id, tool) VALUES (?, ?, ?)",
                    (block["id"], row["request_id"], block.get("name") or "?"),
                )
        touch_session(db, d)
    elif kind == "user":
        touch_session(db, d)
        text = human_prompt_text(d)
        ts = parse_ts(d.get("timestamp"))
        if text and d.get("uuid") and d.get("sessionId") and ts is not None:
            db.execute(
                "INSERT OR IGNORE INTO prompts (uuid, session_id, ts, project, excerpt) VALUES (?, ?, ?, ?, ?)",
                (d["uuid"], d["sessionId"], ts, d.get("cwd"), make_excerpt(text)),
            )
    elif kind == "ai-title" and d.get("sessionId"):
        ensure_session(db, d["sessionId"])
        db.execute("UPDATE sessions SET title = ? WHERE session_id = ?", (d.get("aiTitle"), d["sessionId"]))
    elif kind == "agent-name" and d.get("sessionId"):
        ensure_session(db, d["sessionId"])
        db.execute("UPDATE sessions SET custom_name = ? WHERE session_id = ?", (d.get("agentName"), d["sessionId"]))
    elif kind == "cost-state" and d.get("sessionId"):
        ensure_session(db, d["sessionId"])
        db.execute(
            "UPDATE sessions SET cost_state_usd = ?, lines_added = ?, lines_removed = ?, duration_ms = ?, "
            "api_duration_ms = ? WHERE session_id = ?",
            (
                d.get("totalCostUSD"), d.get("totalLinesAdded"), d.get("totalLinesRemoved"),
                d.get("totalDuration"), d.get("totalAPIDuration"), d["sessionId"],
            ),
        )
    elif kind == "pr-link" and d.get("prUrl") and d.get("sessionId"):
        db.execute(
            "INSERT OR REPLACE INTO prs (url, session_id, number, repository, ts) VALUES (?, ?, ?, ?, ?)",
            (d["prUrl"], d["sessionId"], d.get("prNumber"), d.get("prRepository"), parse_ts(d.get("timestamp"))),
        )


def handle_limit(db, d, _pricing):
    ts = d.get("ts")
    if ts is None:
        return
    db.execute(
        "INSERT OR IGNORE INTO limits (ts, five_pct, five_reset, week_pct, week_reset) VALUES (?, ?, ?, ?, ?)",
        (ts, d.get("five_pct"), d.get("five_reset"), d.get("week_pct"), d.get("week_reset")),
    )


def read_new_lines(db, path, handler, pricing):
    """Processes only the part of a JSONL file appended since the previous run."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0
    known = db.execute("SELECT offset, size, mtime_ns FROM files WHERE path = ?", (str(path),)).fetchone()
    offset = 0
    if known:
        if known[1] == stat.st_size and known[2] == stat.st_mtime_ns:
            return 0
        if stat.st_size >= known[0]:
            offset = known[0]
    count = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                handler(db, d, pricing)
                count += 1
    db.execute(
        "INSERT OR REPLACE INTO files (path, offset, size, mtime_ns) VALUES (?, ?, ?, ?)",
        (str(path), offset, stat.st_size, stat.st_mtime_ns),
    )
    return count


def ingest_subagent_meta(db, projects_dir):
    for meta in projects_dir.glob("*/*/subagents/agent-*.meta.json"):
        agent_id = meta.name[len("agent-"): -len(".meta.json")]
        session_id = meta.parent.parent.name
        try:
            data = json.loads(meta.read_text())
        except (ValueError, OSError):
            continue
        db.execute(
            "INSERT OR REPLACE INTO subagents (agent_id, session_id, agent_type, description) VALUES (?, ?, ?, ?)",
            (agent_id, session_id, data.get("agentType"), data.get("description")),
        )


def ingest_stats_cache(db, path):
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return
    for day in data.get("dailyModelTokens") or []:
        for model, tokens in (day.get("tokensByModel") or {}).items():
            db.execute(
                "INSERT OR REPLACE INTO history_daily (date, model, tokens) VALUES (?, ?, ?)",
                (day.get("date"), model, tokens),
            )


def reprice(db, pricing):
    cursor = db.execute(f"SELECT {', '.join(REQUEST_COLUMNS)} FROM requests")
    updates = []
    for values in cursor.fetchall():
        row = dict(zip(REQUEST_COLUMNS, values))
        updates.append((pricing.cost(row), row["request_id"]))
    db.executemany("UPDATE requests SET cost_usd = ? WHERE request_id = ?", updates)
    return len(updates)


def connect(db_path):
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    return db


def run(db_path, projects_dir, pricing_path, limits_path=None, stats_cache=None, do_reprice=False):
    pricing = Pricing(pricing_path)
    projects_dir = Path(projects_dir)
    db = connect(db_path)
    lines = 0
    try:
        for path in sorted(projects_dir.glob("**/*.jsonl")):
            with db:
                lines += read_new_lines(db, path, handle_line, pricing)
        with db:
            ingest_subagent_meta(db, projects_dir)
            if stats_cache:
                ingest_stats_cache(db, stats_cache)
            if limits_path:
                lines += read_new_lines(db, Path(limits_path), handle_limit, pricing)
            if do_reprice:
                reprice(db, pricing)
    finally:
        db.close()
    return lines


def main():
    home = Path.home() / ".claude"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--projects", default=str(home / "projects"))
    parser.add_argument("--pricing", default=str(Path(__file__).with_name("pricing.json")))
    parser.add_argument("--limits")
    parser.add_argument("--stats-cache", default=str(home / "stats-cache.json"))
    parser.add_argument("--reprice", action="store_true", help="recompute the cost of all stored requests from pricing.json")
    args = parser.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    lines = run(args.db, args.projects, args.pricing, args.limits, args.stats_cache, args.reprice)
    print(f"{lines} new lines ingested", file=sys.stderr)


if __name__ == "__main__":
    main()
