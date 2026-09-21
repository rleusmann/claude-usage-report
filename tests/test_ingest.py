import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import ingest  # noqa: E402

PRICING = ROOT / "lib" / "pricing.json"
SESSION = "11111111-1111-1111-1111-111111111111"


def assistant(request_id, ts, output, tools=(), model="claude-opus-5", **extra):
    content = [{"type": "tool_use", "id": f"toolu_{request_id}_{name}", "name": name, "input": {}} for name in tools]
    d = {
        "type": "assistant",
        "sessionId": SESSION,
        "requestId": request_id,
        "timestamp": ts,
        "cwd": "/Users/test/Code/demo",
        "gitBranch": "main",
        "effort": "high",
        "message": {
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 100000,
                "output_tokens": output,
                "output_tokens_details": {"thinking_tokens": 5},
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000},
                "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            },
        },
    }
    d.update(extra)
    return d


def user_prompt(uuid, ts, content, **extra):
    d = {
        "type": "user",
        "uuid": uuid,
        "sessionId": SESSION,
        "timestamp": ts,
        "cwd": "/Users/test/Code/demo",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": content},
    }
    d.update(extra)
    return d


def write_jsonl(path, records, partial_tail=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        if partial_tail:
            fh.write(partial_tail)


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.projects = base / "projects"
        self.db_path = base / "usage.db"
        self.transcript = self.projects / "-Users-test-Code-demo" / f"{SESSION}.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_ingest(self, **kwargs):
        return ingest.run(str(self.db_path), str(self.projects), str(PRICING), **kwargs)

    def query(self, sql, *params):
        db = sqlite3.connect(self.db_path)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def test_duplicate_request_lines_are_counted_once_with_final_usage(self):
        write_jsonl(self.transcript, [
            assistant("req_1", "2026-09-17T10:00:00Z", 50, tools=["Read"]),
            assistant("req_1", "2026-09-17T10:00:01Z", 400, tools=["Bash"]),
        ])
        self.run_ingest()
        rows = self.query("SELECT output_tokens, ts FROM requests")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 400)
        tools = self.query("SELECT tool FROM tool_uses WHERE request_id = 'req_1' ORDER BY tool")
        self.assertEqual([t[0] for t in tools], ["Bash", "Read"])

    def test_cost_uses_cache_write_duration_and_model_price(self):
        write_jsonl(self.transcript, [assistant("req_1", "2026-09-17T10:00:00Z", 1000)])
        self.run_ingest()
        cost = self.query("SELECT cost_usd FROM requests")[0][0]
        expected = (10 * 5.0 + 1000 * 10.0 + 100000 * 0.5 + 1000 * 25.0) / 1_000_000
        self.assertAlmostEqual(cost, expected, places=9)

    def test_fast_mode_and_web_search_are_priced(self):
        record = assistant("req_1", "2026-09-17T10:00:00Z", 1000)
        record["message"]["usage"]["speed"] = "fast"
        record["message"]["usage"]["server_tool_use"]["web_search_requests"] = 3
        write_jsonl(self.transcript, [record])
        self.run_ingest()
        cost = self.query("SELECT cost_usd FROM requests")[0][0]
        expected = 2 * (10 * 5.0 + 1000 * 10.0 + 100000 * 0.5 + 1000 * 25.0) / 1_000_000 + 0.03
        self.assertAlmostEqual(cost, expected, places=9)

    def test_unknown_model_has_no_cost_and_synthetic_is_skipped(self):
        write_jsonl(self.transcript, [
            assistant("req_1", "2026-09-17T10:00:00Z", 10, model="claude-unknown-9"),
            assistant("req_2", "2026-09-17T10:00:00Z", 10, model="<synthetic>"),
        ])
        self.run_ingest()
        self.assertEqual(self.query("SELECT request_id, cost_usd FROM requests"), [("req_1", None)])

    def test_only_human_prompts_are_stored_and_masked(self):
        write_jsonl(self.transcript, [
            user_prompt("u1", "2026-09-17T10:00:00Z", "Please deploy with token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 and " + "x" * 200),
            user_prompt("u2", "2026-09-17T10:01:00Z", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]),
            user_prompt("u3", "2026-09-17T10:02:00Z", "<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
            user_prompt("u4", "2026-09-17T10:03:00Z", "<command-name>/usage-report</command-name><command-args>week</command-args>"),
            user_prompt("u5", "2026-09-17T10:04:00Z", "Meta", isMeta=True),
            user_prompt("u6", "2026-09-17T10:05:00Z", "<local-command-stdout>x</local-command-stdout>", origin=None),
        ])
        self.run_ingest()
        prompts = dict(self.query("SELECT uuid, excerpt FROM prompts"))
        self.assertEqual(set(prompts), {"u1", "u4"})
        self.assertNotIn("ghp_", prompts["u1"])
        self.assertIn("token=•••", prompts["u1"])
        self.assertLessEqual(len(prompts["u1"]), ingest.EXCERPT_LEN)
        self.assertTrue(prompts["u1"].endswith("…"))
        self.assertEqual(prompts["u4"], "/usage-report week")

    def test_session_metadata_title_cost_state_and_prs(self):
        write_jsonl(self.transcript, [
            user_prompt("u1", "2026-09-17T10:00:00Z", "Hello", entrypoint="cli", version="2.1.274"),
            assistant("req_1", "2026-09-17T10:05:00Z", 10),
            {"type": "ai-title", "aiTitle": "Demo title", "sessionId": SESSION},
            {"type": "cost-state", "sessionId": SESSION, "totalCostUSD": 1.5, "totalLinesAdded": 7,
             "totalLinesRemoved": 2, "totalDuration": 60000, "totalAPIDuration": 30000},
            {"type": "pr-link", "sessionId": SESSION, "prNumber": 4, "prUrl": "https://github.com/x/y/pull/4",
             "prRepository": "x/y", "timestamp": "2026-09-17T10:06:00Z"},
        ])
        self.run_ingest()
        session = self.query(
            "SELECT project, title, first_ts < last_ts, cost_state_usd, lines_added, lines_removed, entrypoint FROM sessions"
        )
        self.assertEqual(session, [("/Users/test/Code/demo", "Demo title", 1, 1.5, 7, 2, "cli")])
        self.assertEqual(self.query("SELECT number, repository FROM prs"), [(4, "x/y")])

    def test_session_times_are_set_when_title_arrives_first(self):
        write_jsonl(self.transcript, [
            {"type": "ai-title", "aiTitle": "Early title", "sessionId": SESSION},
            user_prompt("u1", "2026-09-17T10:00:00Z", "Hello"),
            assistant("req_1", "2026-09-17T10:05:00Z", 10),
        ])
        self.run_ingest()
        first, last = self.query("SELECT first_ts, last_ts FROM sessions")[0]
        self.assertIsNotNone(first)
        self.assertEqual(last - first, 300)

    def test_subagent_requests_are_linked_to_parent_session(self):
        agent_file = self.transcript.parent / SESSION / "subagents" / "agent-abc123.jsonl"
        write_jsonl(agent_file, [assistant("req_sub", "2026-09-17T10:00:00Z", 10, isSidechain=True, agentId="abc123")])
        (agent_file.parent / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "Search"}))
        self.run_ingest()
        self.assertEqual(self.query("SELECT session_id, agent_id FROM requests"), [(SESSION, "abc123")])
        self.assertEqual(self.query("SELECT session_id, agent_type FROM subagents"), [(SESSION, "Explore")])

    def test_incremental_ingest_reads_only_appended_complete_lines(self):
        write_jsonl(self.transcript, [assistant("req_1", "2026-09-17T10:00:00Z", 10)], partial_tail='{"type": "assis')
        self.assertEqual(self.run_ingest(), 1)
        self.assertEqual(self.run_ingest(), 0)
        with open(self.transcript, "a") as fh:
            fh.write('tant"}\n')
        write_jsonl(self.transcript, [assistant("req_2", "2026-09-17T10:01:00Z", 10)])
        self.assertEqual(self.run_ingest(), 2)
        self.assertEqual(self.query("SELECT COUNT(*) FROM requests")[0][0], 2)

    def test_data_survives_deleted_transcripts(self):
        write_jsonl(self.transcript, [assistant("req_1", "2026-09-17T10:00:00Z", 10)])
        self.run_ingest()
        self.transcript.unlink()
        self.run_ingest()
        self.assertEqual(self.query("SELECT COUNT(*) FROM requests")[0][0], 1)

    def test_limits_and_stats_cache_history(self):
        limits = Path(self.tmp.name) / "limits.jsonl"
        write_jsonl(limits, [
            {"ts": 1789650000, "five_pct": 12.5, "five_reset": 1789660000, "week_pct": 40, "week_reset": 1790000000},
            {"ts": 1789650060, "five_pct": 13, "five_reset": 1789660000, "week_pct": 40, "week_reset": 1790000000},
        ])
        stats = Path(self.tmp.name) / "stats-cache.json"
        stats.write_text(json.dumps({"dailyModelTokens": [{"date": "2026-04-30", "tokensByModel": {"claude-sonnet-4-6": 99}}]}))
        self.projects.mkdir(parents=True)
        self.run_ingest(limits_path=str(limits), stats_cache=str(stats))
        self.assertEqual(self.query("SELECT COUNT(*) FROM limits")[0][0], 2)
        self.assertEqual(self.query("SELECT date, model, tokens FROM history_daily"), [("2026-04-30", "claude-sonnet-4-6", 99)])

    def test_reprice_updates_existing_costs(self):
        write_jsonl(self.transcript, [assistant("req_1", "2026-09-17T10:00:00Z", 10)])
        self.run_ingest()
        db = sqlite3.connect(self.db_path)
        db.execute("UPDATE requests SET cost_usd = 999")
        db.commit()
        db.close()
        self.run_ingest(do_reprice=True)
        self.assertLess(self.query("SELECT cost_usd FROM requests")[0][0], 1)


class MaskingTest(unittest.TestCase):
    def test_masks_common_secret_shapes(self):
        samples = [
            "sk-ant-api03-AbCdEfGhIjKlMnOp",
            "Authorization: Bearer abc.def.ghi",
            "password: hunter2",
            "https://user:s3cret@example.com/repo",
            "AKIAABCDEFGHIJKLMNOP",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
            "key 9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc",
        ]
        for sample in samples:
            masked = ingest.mask_secrets(sample)
            self.assertIn(ingest.MASK, masked, sample)
        self.assertNotIn("hunter2", ingest.mask_secrets("password: hunter2"))
        self.assertNotIn("s3cret", ingest.mask_secrets("https://user:s3cret@example.com"))

    def test_keeps_normal_text_and_paths(self):
        text = "Check /Users/alice/Code/example/apps/monitoring/alloy/values.yaml please"
        self.assertEqual(ingest.mask_secrets(text), text)


if __name__ == "__main__":
    unittest.main()
