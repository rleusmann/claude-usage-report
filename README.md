# claude-usage-report

A Claude Code plugin that turns your local session transcripts into a compact SQLite database and a self-contained HTML report: token usage, what it would have cost on the API, subscription limit utilization, and where the usage goes.

Everything stays on your machine. No network access at runtime; the charting library is vendored.

## What the report shows

- **Headline numbers** – API value today, last 7/30 days, total; sessions and tokens.
- **Limits** – 5-hour and weekly utilization over time, projection to the next reset, unused share per weekly window, and an estimate of the weekly limit in API dollars.
- **Daily spend** by model.
- **Projects** – cost, tokens, sessions, changed lines and PRs per working directory.
- **Usage patterns** – weekday × hour heatmap.
- **Cache efficiency** – cost breakdown, savings versus no caching, daily hit ratio, sessions with the most expensive cache writes.
- **Tools, MCP servers and subagents.**
- **Model and effort** – cost share, average cost per prompt by effort level.
- **Output** – lines changed, linked PRs, API value per 100 lines.
- **Outliers** – most expensive sessions and prompts.
- **Long-term history** – monthly tokens, extended with `~/.claude/stats-cache.json`.
- **All sessions** – sortable and filterable table.

"API value" is computed from token counts and Anthropic list prices (`lib/pricing.json`). With a Pro/Max subscription you do not pay it; it shows what the same usage would cost on the API.

## How it works

```
~/.claude/projects/**/*.jsonl ─┐
~/.claude/stats-cache.json ────┼─> lib/ingest.py ─> usage.db ─> lib/render.py ─> report.html
status line ─> limits.jsonl ───┘
```

- `lib/ingest.py` reads transcripts incrementally (byte offsets per file), deduplicates API requests by request id and stores per-request token counts, cost, tools, prompt excerpts and session metadata. A full first import of ~160 MB of transcripts takes about a second and yields a ~4 MB database; later runs only read appended lines.
- Data in `usage.db` survives Claude Code's transcript cleanup (`cleanupPeriodDays`), so the history keeps growing after old transcripts are deleted.
- Prompt excerpts are truncated to 120 characters and masked for secret-like patterns (API keys, tokens, `password=…`, URLs with credentials, long random strings). Claude's responses and tool output are never stored.
- `lib/render.py` aggregates the database and writes a single HTML file (light and dark mode).
- Hooks run `bin/usage-report-update` detached at `SessionEnd` and, throttled to every 30 minutes, at `Stop`.

Data lives in the plugin data directory (`~/.claude/plugins/data/usage-report-<marketplace>/`); `bin/usage-report-update --print-paths` prints the exact locations.

## Installation

```bash
claude plugin marketplace add ~/Code/claude-usage-report
claude plugin install usage-report@claude-usage-report
```

Restart Claude Code so the hooks load. The first import happens at the end of the next session, or immediately with `/usage-report`.

### Recording limits (optional, recommended)

Plugins cannot configure the status line, and rate-limit data is only available to the status line. Add this to your status line script, right after it reads its JSON input into `$input`:

```bash
printf '%s' "$input" | ~/Code/claude-usage-report/bin/usage-report-log-limits >/dev/null 2>&1 &
```

It appends a snapshot only when a value changed. Without it, the limit section stays empty and everything else still works.

## Usage

- `/usage-report` – refresh and open the report, or ask questions about your usage; the skill knows the database schema.
- `bin/usage-report-update [--open] [--reprice] [--print-paths]` – manual refresh. Use `--reprice` after editing `lib/pricing.json`.

## Accuracy notes

- Costs are list prices for input, 5-minute and 1-hour cache writes, cache reads, output, fast mode and web search. Batch discounts, data residency multipliers and negotiated pricing are not modeled.
- Compared with Claude Code's own per-session `cost-state`, the computed value is typically 2–7 % lower: small auxiliary requests (for example WebFetch summarization) are not written to transcripts. For resumed sessions the computed value is higher, because `cost-state` only covers the last process.
- The weekly-limit estimate divides the API value spent in a weekly window by the percentage used; it needs at least one window with ≥ 5 % usage.

## Development

```bash
python3 -m unittest discover -s tests
python3 lib/ingest.py --db /tmp/usage.db && python3 lib/render.py --db /tmp/usage.db --out /tmp/report.html
```

Requirements: Python 3.9+ (standard library only), `jq` for the limit logger, macOS or Linux.
