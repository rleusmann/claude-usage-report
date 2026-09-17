#!/usr/bin/env python3
"""Renders a self-contained HTML report from usage.db."""

import argparse
import bisect
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest import Pricing  # noqa: E402

LIB = Path(__file__).resolve().parent
WEEK = 7 * 24 * 3600
FIVE_HOURS = 5 * 3600
MAX_MODEL_SERIES = 5
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def local(ts):
    return datetime.fromtimestamp(ts)


def day_key(ts):
    return local(ts).date().isoformat()


def project_name(path):
    if not path:
        return "(unknown)"
    home = str(Path.home())
    if path.rstrip("/") == home:
        return "~"
    return os.path.basename(path.rstrip("/")) or path


def short_model(model):
    return model.replace("claude-", "").split("-2025")[0].split("-2026")[0]


def load(db):
    db.row_factory = sqlite3.Row
    q = lambda sql: [dict(r) for r in db.execute(sql).fetchall()]  # noqa: E731
    return {
        "requests": q("SELECT * FROM requests ORDER BY ts"),
        "prompts": q("SELECT * FROM prompts ORDER BY ts"),
        "sessions": q("SELECT * FROM sessions"),
        "tools": q("SELECT request_id, tool FROM tool_uses"),
        "subagents": q("SELECT * FROM subagents"),
        "prs": q("SELECT * FROM prs"),
        "limits": q("SELECT * FROM limits ORDER BY ts"),
        "history": q("SELECT * FROM history_daily ORDER BY date"),
    }


def tokens_of(r):
    return r["input_tokens"] + r["cache_write_5m"] + r["cache_write_1h"] + r["cache_read"] + r["output_tokens"]


def cost_of(r):
    return r["cost_usd"] or 0.0


def attribute_prompts(requests, prompts):
    """Attributes each API request to the latest human prompt of the same session."""
    by_session = defaultdict(list)
    for p in prompts:
        by_session[p["session_id"]].append(p)
    stamps = {sid: [p["ts"] for p in ps] for sid, ps in by_session.items()}
    result = defaultdict(lambda: {"cost": 0.0, "requests": 0})
    for r in requests:
        ts_list = stamps.get(r["session_id"])
        if not ts_list:
            continue
        idx = bisect.bisect_right(ts_list, r["ts"]) - 1
        if idx < 0:
            continue
        prompt = by_session[r["session_id"]][idx]
        entry = result[prompt["uuid"]]
        entry["cost"] += cost_of(r)
        entry["requests"] += 1
    return result


def build_daily(requests, today):
    if not requests:
        return {"days": [], "models": [], "series": {}}
    model_cost = Counter()
    for r in requests:
        model_cost[short_model(r["model"])] += cost_of(r)
    top = [m for m, _ in model_cost.most_common(MAX_MODEL_SERIES)]
    others = len(model_cost) > MAX_MODEL_SERIES
    first = local(requests[0]["ts"]).date()
    days = []
    d = first
    while d <= today:
        days.append(d.isoformat())
        d += timedelta(days=1)
    index = {k: i for i, k in enumerate(days)}
    names = top + (["Other"] if others else [])
    series = {m: [0.0] * len(days) for m in names}
    tokens = [0] * len(days)
    for r in requests:
        i = index.get(day_key(r["ts"]))
        if i is None:
            continue
        m = short_model(r["model"])
        series[m if m in top else "Other"][i] += cost_of(r)
        tokens[i] += tokens_of(r)
    return {
        "days": days,
        "models": names,
        "series": {m: [round(v, 4) for v in vals] for m, vals in series.items()},
        "tokens": tokens,
    }


def build_history(history, requests):
    per_day = defaultdict(int)
    source = {}
    for h in history:
        per_day[h["date"]] += h["tokens"]
        source[h["date"]] = "stats-cache"
    transcript_days = defaultdict(int)
    for r in requests:
        transcript_days[day_key(r["ts"])] += r["input_tokens"] + r["output_tokens"]
    first_transcript = min(transcript_days) if transcript_days else None
    for day, tokens in transcript_days.items():
        per_day[day] = tokens
        source[day] = "transcripts"
    if first_transcript:
        for day in [d for d in per_day if d < first_transcript and source[d] == "transcripts"]:
            del per_day[day]
    if not per_day:
        return {"months": [], "tokens": [], "sources": []}
    months = defaultdict(lambda: [0, set()])
    for day, tokens in per_day.items():
        months[day[:7]][0] += tokens
        months[day[:7]][1].add(source[day])
    start = datetime.strptime(min(months), "%Y-%m")
    end = datetime.strptime(max(months), "%Y-%m")
    keys = []
    cur = start
    while cur <= end:
        keys.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {
        "months": keys,
        "tokens": [months[k][0] if k in months else 0 for k in keys],
        "sources": [", ".join(sorted(months[k][1])) if k in months else "no data" for k in keys],
    }


def window_groups(limits, reset_key, pct_key, length):
    groups = defaultdict(list)
    for row in limits:
        reset = row[reset_key]
        pct = row[pct_key]
        if reset is None or pct is None:
            continue
        groups[round(reset / 60) * 60].append((row["ts"], pct))
    out = []
    for reset, points in sorted(groups.items()):
        out.append({"reset": reset, "start": reset - length, "peak": max(p for _, p in points), "points": sorted(points)})
    return out


def forecast(window, now, length):
    if not window or window["reset"] <= now:
        return None
    points = window["points"]
    ts, pct = points[-1]
    elapsed = max(ts - window["start"], 60)
    pace = pct / elapsed
    recent_pace = None
    for p_ts, p_pct in points:
        if ts - p_ts <= min(24 * 3600, length / 5) and ts - p_ts >= min(3600, length / 20):
            recent_pace = (pct - p_pct) / (ts - p_ts)
            break
    remaining = window["reset"] - now
    projected = pct + pace * remaining
    eta = None
    if pace > 0 and projected >= 100:
        eta = now + (100 - pct) / pace
    return {
        "pct": pct,
        "reset": window["reset"],
        "projected": round(min(projected, 999), 1),
        "eta": eta,
        "recent_projected": round(min(pct + recent_pace * remaining, 999), 1) if recent_pace is not None else None,
        "as_of": ts,
    }


def build_limits(limits, requests, now):
    weeks = window_groups(limits, "week_reset", "week_pct", WEEK)
    fives = window_groups(limits, "five_reset", "five_pct", FIVE_HOURS)
    stamps = [r["ts"] for r in requests]
    costs = [cost_of(r) for r in requests]
    prefix = [0.0]
    for c in costs:
        prefix.append(prefix[-1] + c)

    def cost_between(a, b):
        return prefix[bisect.bisect_right(stamps, b)] - prefix[bisect.bisect_left(stamps, a)]

    week_rows = []
    for w in weeks:
        cost = cost_between(w["start"], min(w["reset"], now))
        week_rows.append({
            "reset": w["reset"],
            "peak": round(w["peak"], 1),
            "closed": w["reset"] <= now,
            "cost": round(cost, 2),
            "usd_per_pct": round(cost / w["peak"], 3) if w["peak"] >= 5 else None,
            "first_seen": w["points"][0][0],
        })
    per_pct = [w["usd_per_pct"] for w in week_rows if w["usd_per_pct"]]
    closed_fives = [f for f in fives if f["reset"] <= now]
    return {
        "series": [
            {"ts": row["ts"], "five": row["five_pct"], "five_reset": row["five_reset"], "week": row["week_pct"], "week_reset": row["week_reset"]}
            for row in limits
        ],
        "weeks": week_rows,
        "fives": {
            "count": len(closed_fives),
            "avg_peak": round(sum(f["peak"] for f in closed_fives) / len(closed_fives), 1) if closed_fives else None,
            "over_90": sum(1 for f in closed_fives if f["peak"] >= 90),
            "hit_100": sum(1 for f in closed_fives if f["peak"] >= 100),
            "recent": [{"reset": f["reset"], "peak": round(f["peak"], 1)} for f in fives[-40:]],
        },
        "forecast_week": forecast(weeks[-1] if weeks else None, now, WEEK),
        "forecast_five": forecast(fives[-1] if fives else None, now, FIVE_HOURS),
        "week_limit_usd": round(100 * sum(per_pct) / len(per_pct), 0) if per_pct else None,
        "since": limits[0]["ts"] if limits else None,
    }


def build_cache(requests, pricing):
    split = Counter()
    no_cache = 0.0
    written_1h = written_5m = 0
    daily = defaultdict(lambda: [0, 0])
    session_writes = Counter()
    for r in requests:
        price = pricing.lookup(r["model"])
        if not price:
            continue
        mult = pricing.fast if r.get("speed") == "fast" else 1.0
        unit = mult / 1_000_000
        split["Input"] += r["input_tokens"] * price["input"] * unit
        split["Cache writes (5 min)"] += r["cache_write_5m"] * price["cache_write_5m"] * unit
        split["Cache writes (1 h)"] += r["cache_write_1h"] * price["cache_write_1h"] * unit
        split["Cache reads"] += r["cache_read"] * price["cache_read"] * unit
        split["Output"] += r["output_tokens"] * price["output"] * unit
        split["Web search"] += r["web_search"] * pricing.web_search
        no_cache += (r["input_tokens"] + r["cache_write_5m"] + r["cache_write_1h"] + r["cache_read"]) * price["input"] * unit
        no_cache += r["output_tokens"] * price["output"] * unit + r["web_search"] * pricing.web_search
        written_1h += r["cache_write_1h"]
        written_5m += r["cache_write_5m"]
        all_input = r["input_tokens"] + r["cache_write_5m"] + r["cache_write_1h"] + r["cache_read"]
        d = daily[day_key(r["ts"])]
        d[0] += r["cache_read"]
        d[1] += all_input
        session_writes[r["session_id"]] += (
            r["cache_write_5m"] * price["cache_write_5m"] + r["cache_write_1h"] * price["cache_write_1h"]
        ) * unit
    actual = sum(split.values())
    return {
        "split": [{"name": k, "value": round(v, 2)} for k, v in split.items() if v > 0],
        "actual": round(actual, 2),
        "without_cache": round(no_cache, 2),
        "share_1h": round(written_1h / (written_1h + written_5m), 3) if written_1h + written_5m else None,
        "daily": [{"day": k, "ratio": round(v[0] / v[1], 4) if v[1] else None} for k, v in sorted(daily.items())],
        "session_writes": session_writes,
    }


def build(db_path, pricing_path, now=None):
    now = now or time.time()
    today = local(now).date()
    db = sqlite3.connect(db_path)
    try:
        data = load(db)
    finally:
        db.close()
    pricing = Pricing(pricing_path)
    requests = data["requests"]
    sessions = {s["session_id"]: s for s in data["sessions"]}
    subagents = {a["agent_id"]: a for a in data["subagents"]}

    def since(days):
        start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time()).timestamp()
        return [r for r in requests if r["ts"] >= start]

    total_cost = sum(cost_of(r) for r in requests)
    last30 = since(30)
    kpis = {
        "today": round(sum(cost_of(r) for r in since(1)), 2),
        "week": round(sum(cost_of(r) for r in since(7)), 2),
        "month": round(sum(cost_of(r) for r in last30), 2),
        "total": round(total_cost, 2),
        "sessions_30": len({r["session_id"] for r in last30}),
        "tokens_30": sum(tokens_of(r) for r in last30),
        "requests": len(requests),
        "first": requests[0]["ts"] if requests else None,
    }

    per_session = defaultdict(lambda: {"cost": 0.0, "requests": 0, "tokens": 0, "models": Counter(), "sub_cost": 0.0,
                                       "first": None, "last": None})
    per_project = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "requests": 0, "sessions": set()})
    heat = [[0.0] * 24 for _ in range(7)]
    per_model = defaultdict(lambda: {"cost": 0.0, "requests": 0, "tokens": 0})
    per_effort = defaultdict(lambda: {"cost": 0.0, "requests": 0})
    per_agent_type = defaultdict(lambda: {"cost": 0.0, "requests": 0})
    for r in requests:
        c = cost_of(r)
        s = per_session[r["session_id"]]
        s["cost"] += c
        s["requests"] += 1
        s["tokens"] += tokens_of(r)
        s["first"] = s["first"] if s["first"] is not None else r["ts"]
        s["last"] = r["ts"]
        s["models"][short_model(r["model"])] += c
        session_project = (sessions.get(r["session_id"]) or {}).get("project") or r["project"]
        p = per_project[project_name(session_project)]
        p["cost"] += c
        p["tokens"] += tokens_of(r)
        p["requests"] += 1
        p["sessions"].add(r["session_id"])
        dt = local(r["ts"])
        heat[dt.weekday()][dt.hour] += c
        m = per_model[short_model(r["model"])]
        m["cost"] += c
        m["requests"] += 1
        m["tokens"] += tokens_of(r)
        e = per_effort[r["effort"] or "unknown"]
        e["cost"] += c
        e["requests"] += 1
        if r["agent_id"]:
            s["sub_cost"] += c
            kind = (subagents.get(r["agent_id"]) or {}).get("agent_type") or "subagent"
            per_agent_type[kind]["cost"] += c
            per_agent_type[kind]["requests"] += 1

    prs_by_session = defaultdict(list)
    for pr in data["prs"]:
        prs_by_session[pr["session_id"]].append(pr)

    attribution = attribute_prompts(requests, data["prompts"])
    prompts_by_session = Counter(p["session_id"] for p in data["prompts"])
    session_rows = []
    for sid, agg in per_session.items():
        meta = sessions.get(sid) or {}
        first_ts = meta.get("first_ts") or agg["first"]
        last_ts = meta.get("last_ts") or agg["last"]
        session_rows.append({
            "id": sid,
            "start": first_ts,
            "project": project_name(meta.get("project")),
            "title": meta.get("custom_name") or meta.get("title") or "(untitled)",
            "minutes": round((last_ts - first_ts) / 60) if first_ts and last_ts else None,
            "model": agg["models"].most_common(1)[0][0] if agg["models"] else None,
            "requests": agg["requests"],
            "prompts": prompts_by_session.get(sid, 0),
            "tokens": agg["tokens"],
            "cost": round(agg["cost"], 2),
            "sub_share": round(agg["sub_cost"] / agg["cost"], 3) if agg["cost"] else 0,
            "added": meta.get("lines_added"),
            "removed": meta.get("lines_removed"),
            "prs": len(prs_by_session.get(sid, [])),
        })
    session_rows.sort(key=lambda s: s["start"] or 0, reverse=True)

    project_rows = []
    for name, agg in per_project.items():
        sids = agg["sessions"]
        project_rows.append({
            "name": name,
            "cost": round(agg["cost"], 2),
            "tokens": agg["tokens"],
            "requests": agg["requests"],
            "sessions": len(sids),
            "added": sum((sessions.get(s) or {}).get("lines_added") or 0 for s in sids),
            "removed": sum((sessions.get(s) or {}).get("lines_removed") or 0 for s in sids),
            "prs": sum(len(prs_by_session.get(s, [])) for s in sids),
        })
    project_rows.sort(key=lambda p: p["cost"], reverse=True)

    prompt_rows = []
    prompt_map = {p["uuid"]: p for p in data["prompts"]}
    for uuid, info in attribution.items():
        p = prompt_map[uuid]
        prompt_rows.append({
            "ts": p["ts"],
            "project": project_name(p["project"]),
            "excerpt": p["excerpt"],
            "cost": round(info["cost"], 2),
            "requests": info["requests"],
            "session": (sessions.get(p["session_id"]) or {}).get("title") or "",
        })
    prompt_rows.sort(key=lambda p: p["cost"], reverse=True)

    effort_prompts = defaultdict(lambda: {"cost": 0.0, "prompts": 0})
    by_session_req = defaultdict(list)
    for r in requests:
        by_session_req[r["session_id"]].append(r)
    prompt_groups = defaultdict(list)
    for p in data["prompts"]:
        prompt_groups[p["session_id"]].append(p["ts"])
    for sid, reqs in by_session_req.items():
        ts_list = prompt_groups.get(sid)
        if not ts_list:
            continue
        buckets = defaultdict(lambda: {"cost": 0.0, "effort": Counter()})
        for r in reqs:
            idx = bisect.bisect_right(ts_list, r["ts"]) - 1
            if idx < 0:
                continue
            buckets[idx]["cost"] += cost_of(r)
            buckets[idx]["effort"][r["effort"] or "unknown"] += 1
        for bucket in buckets.values():
            effort = bucket["effort"].most_common(1)[0][0]
            effort_prompts[effort]["cost"] += bucket["cost"]
            effort_prompts[effort]["prompts"] += 1

    tool_calls = Counter()
    mcp_calls = Counter()
    for t in data["tools"]:
        name = t["tool"]
        if name.startswith("mcp__"):
            parts = name.split("__")
            server = parts[1] if len(parts) > 2 else name
            server = server.replace("plugin_", "").split("_")[-1] if server.startswith("plugin_") else server
            mcp_calls[server] += 1
            tool_calls["MCP: " + server] += 1
        else:
            tool_calls[name] += 1

    cache = build_cache(requests, pricing)
    rebuilds = []
    for sid, value in cache.pop("session_writes").most_common(8):
        meta = sessions.get(sid) or {}
        total = per_session[sid]["cost"]
        rebuilds.append({
            "title": meta.get("custom_name") or meta.get("title") or sid[:8],
            "project": project_name(meta.get("project")),
            "write_cost": round(value, 2),
            "share": round(value / total, 3) if total else None,
        })
    cache["top_writes"] = rebuilds

    lines_added = sum(s["added"] or 0 for s in session_rows)
    lines_removed = sum(s["removed"] or 0 for s in session_rows)
    sub_cost = sum(v["cost"] for v in per_agent_type.values())

    return {
        "generated": now,
        "kpis": kpis,
        "daily": build_daily(requests, today),
        "history": build_history(data["history"], requests),
        "limits": build_limits(data["limits"], requests, now),
        "projects": project_rows,
        "heatmap": {
            "weekdays": WEEKDAYS,
            "cells": [[h, d, round(heat[d][h], 2)] for d in range(7) for h in range(24)],
            "max": round(max(max(row) for row in heat), 2),
        },
        "cache": cache,
        "tools": [{"name": k, "calls": v} for k, v in tool_calls.most_common(15)],
        "mcp": [{"name": k, "calls": v} for k, v in mcp_calls.most_common()],
        "subagents": {
            "share": round(sub_cost / total_cost, 3) if total_cost else 0,
            "by_type": sorted(
                ({"type": k, "cost": round(v["cost"], 2), "requests": v["requests"]} for k, v in per_agent_type.items()),
                key=lambda x: x["cost"], reverse=True,
            ),
        },
        "models": sorted(
            ({"model": k, "cost": round(v["cost"], 2), "requests": v["requests"], "tokens": v["tokens"]} for k, v in per_model.items()),
            key=lambda x: x["cost"], reverse=True,
        ),
        "effort": sorted(
            (
                {
                    "effort": k,
                    "cost": round(v["cost"], 2),
                    "requests": v["requests"],
                    "prompts": effort_prompts[k]["prompts"],
                    "per_prompt": round(effort_prompts[k]["cost"] / effort_prompts[k]["prompts"], 3)
                    if effort_prompts[k]["prompts"] else None,
                }
                for k, v in per_effort.items()
            ),
            key=lambda x: x["cost"], reverse=True,
        ),
        "outcome": {
            "added": lines_added,
            "removed": lines_removed,
            "prs": len(data["prs"]),
            "cost_per_100_lines": round(100 * total_cost / (lines_added + lines_removed), 2)
            if lines_added + lines_removed else None,
        },
        "top_sessions": sorted(session_rows, key=lambda s: s["cost"], reverse=True)[:10],
        "top_prompts": prompt_rows[:15],
        "sessions": session_rows,
    }


def render(data, out_path):
    template = (LIB / "template.html").read_text()
    echarts = (LIB / "vendor" / "echarts.min.js").read_text()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__ECHARTS__*/", echarts).replace("/*__DATA__*/null", payload)
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(html)
    tmp.replace(out_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pricing", default=str(LIB / "pricing.json"))
    args = parser.parse_args()
    render(build(args.db, args.pricing), args.out)
    print(f"Report written: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
