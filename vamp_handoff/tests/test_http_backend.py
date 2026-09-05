# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Drive the real HttpStreamingBackend and SessionReplayRunner against a
local OpenAI-compatible SSE server (stdlib http.server). No model runs; the
server echoes the expected marker so the runner's TTFT/usage/target logic is
exercised on real sockets and real SSE framing."""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HANDOFF = Path(__file__).resolve().parents[1]
if str(HANDOFF) not in sys.path:
    sys.path.insert(0, str(HANDOFF))

from vamp_cxl.keys import RunKey  # noqa: E402
from vamp_cxl.session_replay import (  # noqa: E402
    HttpStreamingBackend,
    MonotonicClock,
    SessionReplayRunner,
    TraceWriter,
)
from vamp_cxl.session_workload import WorkloadConfig, build_manifest  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []
    worker_map: dict[str, str] = {}

    def log_message(self, *args):  # silence
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        target = self.headers.get("X-Target-Worker", "")
        actual = self.worker_map.get(target, target)
        _Handler.seen.append(
            {
                "target": target,
                "task": self.headers.get("X-Task-Id"),
                "stream": body.get("stream"),
            }
        )
        user = body["messages"][-1]["content"]
        marker = user.split("reply exactly ")[1].split(" ")[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-worker-id", actual)
        self.end_headers()

        def sse(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        sse({"choices": [{"delta": {"role": "assistant"}}]})
        self.wfile.write(b": keep-alive\n\n")
        time.sleep(0.02)
        sse({"choices": [{"delta": {"reasoning_content": "thinking"}}]})
        time.sleep(0.02)
        sse({"choices": [{"delta": {"content": marker}}]})
        sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        sse(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 3,
                    "cached_tokens": 100,
                },
            }
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class HttpBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1/chat/completions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _run(self, cfg, misroute=None):
        _Handler.seen = []
        _Handler.worker_map = misroute or {}
        manifest = build_manifest(cfg)
        backend = HttpStreamingBackend(self.url, cfg.model)
        clock = MonotonicClock()
        trace = TraceWriter()
        runner = SessionReplayRunner(
            manifest, RunKey("http", "t", cfg.seed), backend, clock, trace
        )
        deadline = time.monotonic() + 30
        while not runner.done() and time.monotonic() < deadline:
            runner.step()
            time.sleep(0.002)
        self.assertTrue(runner.done())
        return runner, trace

    def test_streaming_ttft_usage_and_receipt(self):
        cfg = WorkloadConfig(
            sessions=3,
            turns_per_session=2,
            gap_s=0.0,
            seed=5,
            max_outstanding_requests=2,
        )
        runner, trace = self._run(cfg)
        self.assertEqual(len(runner.traces), 6)
        for t in runner.traces.values():
            self.assertEqual(t.status, "ok", t.error)
            self.assertTrue(t.marker_ok)
            self.assertTrue(t.usage_observed)
            self.assertEqual((t.prompt_tokens, t.cached_tokens), (123, 100))
            self.assertIsNotNone(t.first_role_ns)
            self.assertIsNotNone(t.first_reasoning_ns)
            self.assertIsNotNone(t.first_content_ns)
            self.assertLess(t.first_role_ns, t.first_reasoning_ns)
            self.assertLess(t.first_reasoning_ns, t.first_content_ns)
            self.assertGreater(t.ttft_content_ns, t.ttft_reasoning_ns)
            self.assertTrue(t.target_verified, (t.designated_worker, t.actual_worker))
        self.assertEqual(runner.invalid_target, [])
        self.assertEqual({s["target"] for s in _Handler.seen}, {"w0", "w1"})
        self.assertTrue(all(s["stream"] for s in _Handler.seen))
        self.assertTrue(all(s["task"].startswith("http/t/5:") for s in _Handler.seen))
        self.assertGreater(
            sum(1 for e in trace.events if e["event"] == "response_done"), 0
        )

    def test_misrouted_worker_is_flagged_not_assumed(self):
        cfg = WorkloadConfig(sessions=2, turns_per_session=1, gap_s=0.0, seed=6)
        runner, _ = self._run(cfg, misroute={"w1": "w0"})
        flagged = [t for t in runner.traces.values() if not t.target_verified]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(
            (flagged[0].designated_worker, flagged[0].actual_worker), ("w1", "w0")
        )
        self.assertEqual(len(runner.invalid_target), 1)


if __name__ == "__main__":
    unittest.main()
