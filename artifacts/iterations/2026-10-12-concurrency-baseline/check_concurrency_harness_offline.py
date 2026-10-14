"""Offline acceptance checks for the concurrency harness (MockTransport only).

This script imports the harness but never starts Docker, contacts a network service,
loads a model, or changes production code/data. It checks output contracts that must
hold before the separately authorized Docker HTTP run.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

HARNESS_PATH = Path(__file__).with_name("run_concurrency_baseline.py")
spec = importlib.util.spec_from_file_location("concurrency_harness_under_test", HARNESS_PATH)
harness = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(harness)


class HarnessOfflineChecks(unittest.TestCase):
    def test_run_level_rows_feed_summarize_with_timeout_exception(self) -> None:
        class FakeSampler:
            stop_reason = None

        class FakeSession:
            def __init__(self, session_hash: str, *, fail: bool = False):
                self.session_hash = session_hash
                self.fail = fail

            async def call(self, method: str, params: dict) -> dict:
                if self.fail:
                    raise httpx.ReadTimeout("synthetic offline timeout")
                return {
                    "http_status": 200,
                    "rpc": {"result": {"isError": False}},
                    "request_id": "mock-1",
                    "tool": {"request_id": "mock-1", "evidence": [{
                        "text": "synthetic_marker",
                        "library": "synthetic-concurrency",
                        "version": "1.0",
                        "index_target_id": "synthetic-concurrency-1.0-target",
                    }]},
                }

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            benchmark = harness.Benchmark(
                {}, output, FakeSampler(), __import__("threading").Event())
            sessions = [FakeSession("session-ok"), FakeSession("session-timeout", fail=True)]
            summary = asyncio.run(benchmark.run_level(
                "shared", sessions,
                {"query": "synthetic query", "target_id": "synthetic-concurrency-1.0-target",
                 "_expected_marker": "synthetic_marker"},
                2, 2, phase="offline-contract"))

            rows = [json.loads(line) for line in (output / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["successes"], 1)
            self.assertEqual(summary["failures"], 1)
            self.assertEqual(summary["error_categories"], {"client_ReadTimeout": 1})
            self.assertEqual(summary["http_status_counts"], {"200": 1})
            timeout_row = next(row for row in rows if row.get("error_category") == "client_ReadTimeout")
            self.assertIsNone(timeout_row["http_status"])
            self.assertIsNotNone(timeout_row["latency_seconds"])
            self.assertEqual(summary["latency_seconds"]["p50_empirical"],
                             sorted(row["latency_seconds"] for row in rows)[0])
            self.assertEqual(summary["start_utc"], rows[0]["wire_start_utc"])
            self.assertEqual(summary["end_utc"], rows[-1]["end_utc"])

    def test_nearest_rank_percentiles_and_failed_http_response_accounting(self) -> None:
        sampler = harness.Sampler({}, Path(tempfile.gettempdir()) / "unused-concurrency-sampler.jsonl")
        benchmark = harness.Benchmark({}, Path(tempfile.gettempdir()), sampler, __import__("threading").Event())
        rows = []
        for index in range(16):
            success = index != 15
            rows.append({
                "success": success,
                "error_category": None if success else "http_503",
                # A completed HTTP 503 has a response latency. Client exceptions
                # should instead omit latency_seconds and remain outside this distribution.
                "latency_seconds": float(index + 1),
                "start_utc": f"2026-10-12T00:00:{index:02d}Z",
                "end_utc": f"2026-10-12T00:00:{index + 1:02d}Z",
                "server_timing_seconds": {},
            })
        summary = benchmark.summarize("shared", 8, rows, 10.0, 14.0)
        self.assertEqual(summary["requests"], 16)
        self.assertEqual(summary["successes"], 15)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["failure_ratio"], 0.0625)
        self.assertEqual(summary["requests_per_second"], 3.75)
        self.assertEqual(summary["latency_seconds"]["p50_empirical"], 8.0)
        self.assertEqual(summary["latency_seconds"]["p95_empirical"], 16.0)
        self.assertEqual(summary["latency_seconds"]["p99_empirical"], 16.0)
        self.assertEqual(summary["successful_latency_seconds"]["p99_empirical"], 15.0)
        self.assertIn("n=16", summary["successful_latency_seconds"]["percentile_note"])

    def test_run_level_rows_summarize_wire_timestamps(self) -> None:
        class FakeSession:
            session_hash = "session-sha"

            async def call(self, method, params):
                return {"http_status": 200, "rpc": {"result": {"isError": False}},
                        "tool": {"request_id": "mock-request", "evidence": [{
                            "text": "synthetic_concurrency_evidence", "library": "synthetic-concurrency",
                            "version": "1.0", "index_target_id": "synthetic-concurrency-1.0-target"}]}}

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sampler = harness.Sampler({}, root / "resources.jsonl")
                benchmark = harness.Benchmark({}, root, sampler, __import__("threading").Event())
                return await benchmark.run_level(
                    "shared", [FakeSession()], {"query": "synthetic", "target_id": "synthetic-concurrency-1.0-target",
                                                "_expected_marker": "synthetic_concurrency_evidence"},
                    1, 1, phase="single")

        summary = asyncio.run(exercise())
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["successes"], 1)
        self.assertTrue(summary["start_utc"])
        self.assertTrue(summary["end_utc"])

    def test_managed_response_must_match_expected_fixture_version(self) -> None:
        class FakeSession:
            session_hash = "session-sha"

            async def call(self, method, params):
                return {"http_status": 200, "rpc": {"result": {"isError": False}},
                        "tool": {"request_id": "mock-request", "managed_file": {
                            "file_id": "fixture-file", "version_id": "stale-version", "version": "v1"},
                            "evidence": [{"text": "synthetic_managed_marker_v1", "file_id": "fixture-file",
                                          "version_id": "stale-version", "version": "v1"}]}}

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sampler = harness.Sampler({}, root / "resources.jsonl")
                benchmark = harness.Benchmark({}, root, sampler, __import__("threading").Event())
                return await benchmark.run_level(
                    "managed", [FakeSession()], {"query": "synthetic", "file_id": "fixture-file",
                                                  "_expected_marker": "synthetic_managed_marker_v1",
                                                  "_expected_version_id": "expected-version"},
                    1, 1, phase="single")

        summary = asyncio.run(exercise())
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["error_categories"], {"expected_evidence_missing": 1})

    def test_sse_message_parser_accepts_event_and_blank_lines(self) -> None:
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(b"event: message\r\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,"
                     b"\"result\":{\"ok\":true}}\r\n\r\n"),
        )
        self.assertEqual(harness.extract_sse_json(response), {
            "jsonrpc": "2.0", "id": 1, "result": {"ok": True},
        })

    def test_two_account_sessions_bind_tokens_and_close_with_delete(self) -> None:
        seen: list[tuple[str, str, str | None]] = []
        sessions_by_token: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            token = request.headers.get("authorization", "")
            session_id = request.headers.get("mcp-session-id")
            seen.append((request.method, token, session_id))
            if request.method == "DELETE":
                return httpx.Response(200, json={"ok": True})
            body = json.loads(request.content or b"{}")
            method = body.get("method")
            if method == "initialize":
                session = "sess-" + token.rsplit("_", 1)[-1]
                sessions_by_token[token] = session
                return httpx.Response(200, headers={"Mcp-Session-Id": session,
                                                    "content-type": "application/json"},
                                      json={"jsonrpc": "2.0", "id": body.get("id"),
                                            "result": {"protocolVersion": harness.PROTOCOL,
                                                       "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "mock", "version": "1"}}})
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/list":
                return httpx.Response(200, headers={"content-type": "application/json"},
                                      json={"jsonrpc": "2.0", "id": body.get("id"),
                                            "result": {"tools": [{"name": "search_docs"}]}})
            if method == "tools/call":
                return httpx.Response(200, headers={"content-type": "application/json"},
                                      json={"jsonrpc": "2.0", "id": body.get("id"),
                                            "result": {"structuredContent": {"request_id": "mock-r"}}})
            return httpx.Response(400, json={"error": "unexpected method"})

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient
        created_clients: list[httpx.AsyncClient] = []

        def mock_client(**kwargs):
            client = real_async_client(transport=transport, **kwargs)
            created_clients.append(client)
            return client

        async def scenario() -> None:
            with patch.object(harness.httpx, "AsyncClient", side_effect=mock_client):
                first = await harness.McpHttpSession.open("http://mock", "wjm_user_A", "a", 1)
                second = await harness.McpHttpSession.open("http://mock", "wjm_user_B", "b", 1)
                listed = await first.call("tools/list", {})
                self.assertEqual(listed["tool"].get("tools"), [{"name": "search_docs"}])
                result = await first.call("tools/call", {"name": "search_docs", "arguments": {}})
                self.assertEqual(result["http_status"], 200)
                await first.close()
                await first.close()  # close must be idempotent across measured/finally paths
                await second.close()
            self.assertEqual(first.protocol, harness.PROTOCOL)
            self.assertNotEqual(first.session_id, second.session_id)
            self.assertTrue(all(token in {"Bearer wjm_user_A", "Bearer wjm_user_B"} for _, token, _ in seen))
            for token in ("Bearer wjm_user_A", "Bearer wjm_user_B"):
                user_calls = [(method, session) for method, auth, session in seen if auth == token]
                own_session = sessions_by_token[token]
                self.assertTrue(any(method == "POST" and session == own_session for method, session in user_calls))
                self.assertEqual(sum(method == "DELETE" and session == own_session
                                     for method, session in user_calls), 1)
            self.assertTrue(all(client.is_closed for client in created_clients))

        asyncio.run(scenario())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HarnessOfflineChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)

