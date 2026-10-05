"""Process and HTTP checks with a fake launcher; these tests never call a model."""
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-deepseek-test-")
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.patches = [patch.object(bridge, "RUNS", self.runs)]
        for item in self.patches:
            item.start()
        helper = self.root / "fake.py"
        helper.write_text('''import json,sys,time
task=sys.stdin.read()
if 'TEST_SLEEP' in task: time.sleep(10)
def emit(x): print(json.dumps(x,ensure_ascii=False),flush=True)
emit({'type':'session','sessionId':'fake-session'})
emit({'type':'status','phase':'turn_start'})
emit({'type':'thinking','text':'PRIVATE_TEST_REASONING'})
emit({'type':'tool_call','callId':'a','tool':'read','input':{'file':'test.txt'}})
emit({'type':'tool_result','callId':'a','status':'completed','result':'ok'})
emit({'type':'status','phase':'step_end','usage':{'inputTokens':10,'outputTokens':2}})
emit({'type':'status','phase':'turn_end','reason':{'kind':'error' if 'TEST_FAIL' in task else 'completed'}})
if 'TEST_NO_FINAL' not in task: emit({'type':'final','text':'完成：'+task.split('--- Codex task ---')[-1]})
sys.exit(1 if 'TEST_FAIL' in task else 0)
''', encoding="utf-8")
        launcher = self.root / "fake launcher.cmd"
        launcher.write_text('@echo off\n"' + sys.executable + '" "' + str(helper) + '" %*\n', encoding="utf-8")
        self.launcher_patch = patch.object(bridge, "DSH", launcher)
        self.launcher_patch.start()

    def tearDown(self):
        self.launcher_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def task(self, text, seconds=20):
        run_id = "20261005-120000-1234abcd"
        path = self.runs / run_id
        path.mkdir()
        (path / "task.txt").write_text(text, encoding="utf-8")
        bridge.write_json(path / "state.json", {"id": run_id, "created": bridge.stamp(), "status": "queued", "backend": "headless", "workspace": str(self.root), "permission": "workspace-write", "timeout_seconds": seconds})
        return run_id, path

    @unittest.skipUnless(os.name == "nt", "Windows launcher contract")
    def test_success_preserves_stdin_and_filters_reasoning(self):
        run_id, path = self.task('中文任务\n& echo SHELL_MARKER\n$(Get-Date)')
        bridge.worker(run_id)
        value = bridge.snapshot(path)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["session_id"], "fake-session")
        self.assertEqual(value["usage"], {"inputTokens": 10, "outputTokens": 2})
        self.assertIn('$(Get-Date)', (path / "result.md").read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE_TEST_REASONING", (path / "events.jsonl").read_text(encoding="utf-8"))
        self.assertTrue(any(x["type"] == "tool_result" for x in value["events"]))

    @unittest.skipUnless(os.name == "nt", "Windows launcher contract")
    def test_nonzero_exit_with_final_is_failure(self):
        run_id, path = self.task("TEST_FAIL")
        bridge.worker(run_id)
        self.assertEqual(bridge.snapshot(path)["status"], "failed")
        self.assertTrue((path / "result.md").exists())

    @unittest.skipUnless(os.name == "nt", "Windows launcher contract")
    def test_zero_exit_without_final_is_failure(self):
        run_id, path = self.task("TEST_NO_FINAL")
        bridge.worker(run_id)
        self.assertEqual(bridge.snapshot(path)["status"], "failed")

    @unittest.skipUnless(os.name == "nt", "Windows launcher contract")
    def test_timeout_stops_process(self):
        run_id, path = self.task("TEST_SLEEP", seconds=1)
        bridge.worker(run_id)
        state = bridge.snapshot(path)
        self.assertEqual(state["status"], "timed_out")
        self.assertFalse(bridge.worker_alive(state["harness_pid"]))

    @unittest.skipUnless(os.name == "nt", "Windows launcher contract")
    def test_cancel_stops_process(self):
        run_id, path = self.task("TEST_SLEEP")
        (path / "cancel.request").touch()
        bridge.worker(run_id)
        state = bridge.snapshot(path)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(bridge.worker_alive(state["harness_pid"]))

    def test_dead_worker_is_not_left_running(self):
        _, path = self.task("test")
        state = bridge.read_json(path / "state.json")
        state.update(status="running", worker_pid=99999999, created=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=2)).isoformat())
        bridge.write_json(path / "state.json", state)
        with patch.object(bridge, "worker_alive", return_value=False):
            self.assertEqual(bridge.current_state(path)["status"], "failed")

    def test_review_and_diagnostics_are_in_timeline(self):
        _, path = self.task("test")
        bridge.event(path, {"type": "codex", "text": "验收通过"}, "notes.jsonl")
        bridge.event(path, {"type": "diagnostic", "text": "test diagnostic"}, "diagnostics.jsonl")
        self.assertEqual([x["type"] for x in bridge.snapshot(path)["events"]], ["codex", "diagnostic"])

    def test_credentials_redacted(self):
        value = bridge.scrub({"api_key": "FAKE_KEY", "text": "Authorization: Bearer FAKE_TOKEN password=FAKE_PASSWORD sk-abcdefghijklmnop"})
        self.assertEqual(value["api_key"], "[REDACTED]")
        for secret in ["FAKE_TOKEN", "FAKE_PASSWORD", "sk-abcdefghijklmnop"]:
            self.assertNotIn(secret, value["text"])

    @unittest.skipUnless(os.name == "nt", "Windows submission lock")
    def test_submission_is_serialized(self):
        with bridge.submission_lock():
            with self.assertRaises(RuntimeError):
                with bridge.submission_lock():
                    self.fail("Concurrent submit acquired the lock")

    def test_resume_id_cannot_enter_command_line_as_shell_code(self):
        task_file = self.root / "task.txt"
        task_file.write_text("test", encoding="utf-8")
        args = SimpleNamespace(workspace=str(self.root), task_file=str(task_file), session_id="session-test & echo SHOULD_NOT_RUN")
        with self.assertRaisesRegex(ValueError, "session UUID"):
            bridge.submit(args)

    def test_active_parent_workspace_blocks_child_writer(self):
        self.task("test")
        child = self.root / "child"
        child.mkdir()
        task_file = self.root / "task.txt"
        task_file.write_text("test", encoding="utf-8")
        args = SimpleNamespace(workspace=str(child), task_file=str(task_file), session_id=None)
        with self.assertRaisesRegex(RuntimeError, "overlaps active"):
            bridge.submit(args)

    def test_http_api_is_local_and_read_only(self):
        server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(bridge, "PORT", port):
                def get(route, headers=None):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", route, headers=headers or {})
                    response = connection.getresponse()
                    status, data = response.status, response.read()
                    connection.close()
                    return status, data
                status, body = get("/api/health")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["service"], "codex-deepseek")
                self.assertEqual(get("/api/runs", {"Origin": "https://example.invalid"})[0], 403)
                self.assertEqual(get("/api/runs", {"Host": "example.invalid"})[0], 403)
                self.assertEqual(get("/api/run?id=../../private")[0], 404)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("POST", "/api/runs", body="{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 501)
                response.read()
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
