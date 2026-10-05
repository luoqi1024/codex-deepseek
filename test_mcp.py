"""mcp_server 的隔离测试：全部使用 mock 与临时目录，绝不调用真实 DeepSeek，不启动仪表盘。"""
import contextlib
import io
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import bridge
import mcp_server

RUN_ID = "20261005-120000-1234abcd"
OTHER_ID = "20261005-110000-aaaaaaaa"
SESSION = "session-11111111-1111-1111-1111-111111111111"


class _FakeProc:
    pid = 4321


class McpTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-mcp-test-")
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.dsh = self.root / "fake-dsh.cmd"
        self.dsh.write_text("@echo off\n", encoding="utf-8")
        (self.root/'settings.json').write_text('{"backend":"desktop"}',encoding='utf-8')
        (self.root/'worker-instructions.txt').write_text('只完成授权任务。',encoding='utf-8')
        (self.root/'worker-reminder.txt').write_text('遵守原任务规则。',encoding='utf-8')
        def native_status(operation, **args):
            if operation == 'health':
                return {'protocol':'codex-deepseek-desktop-v1','version':1,
                        'revision':5,'native_session':True,'return_control':True}
            if operation == 'status':
                state = bridge.read_json(self.runs/args['task_id']/'state.json',{})
                return {'status':state.get('status'),'control':state.get('control','codex'),
                        'owner':state.get('control','codex'),'session_id':state.get('session_id',SESSION)}
            raise AssertionError('Tests must not execute a native operation: '+operation)
        self.patchers = [
            patch.object(bridge, 'ROOT', self.root),
            patch.object(bridge, "RUNS", self.runs),
            patch('desktop_client.call', side_effect=native_status),
            patch.object(bridge, "DSH", self.dsh),
            patch.object(mcp_server, "STAGING_DIR", self.staging),
        ]
        for item in self.patchers:
            item.start()
        self.server = mcp_server.Server()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    # ------------------------------------------------------------------ 工具
    def run_dir(self, run_id=RUN_ID, state=None, events=None, notes=None, diagnostics=None, result=None):
        path = self.runs / run_id
        path.mkdir(parents=True, exist_ok=True)
        base = {"id": run_id, "title": "示例任务", "workspace": str(self.workspace),
                "created": bridge.stamp(), "status": "completed", "permission": "read-only",
                "timeout_seconds": 60, "profile": bridge.PROFILE,
                "provider": "opencode-go", "model": "deepseek-v4.1-flash"}
        base.update(state or {})
        bridge.write_json(path / "state.json", base)
        for name, records in (("events.jsonl", events), ("notes.jsonl", notes), ("diagnostics.jsonl", diagnostics)):
            if records:
                (path / name).write_text(
                    "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
        if result is not None:
            (path / "result.md").write_text(result, encoding="utf-8")
        return path

    def append_events(self, path, records, trailing_newline=True):
        with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if not trailing_newline:
                handle.write(json.dumps(records[-1], ensure_ascii=False))

    def request(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            message["id"] = request_id
        if params is not None:
            message["params"] = params
        return mcp_server.dispatch(message, self.server)

    def call(self, name, arguments, request_id=1):
        response = self.request("tools/call", {"name": name, "arguments": arguments}, request_id)
        self.assertIn("result", response, response)
        return response["result"]

    def tool(self, name, arguments):
        result = self.call(name, arguments)
        return result["content"][0]["text"], result["structuredContent"], bool(result.get("isError"))

    def status(self, arguments):
        text, structured, is_error = self.tool("deepseek_status", arguments)
        self.assertFalse(is_error, text)
        return text, structured

    def submit_with_mocks(self, arguments):
        """执行 deepseek_submit：mock 调起 worker 的 Popen，并断言不会启动仪表盘。"""
        arguments = dict(arguments)
        if not arguments.get('parent_task_id'):
            arguments.setdefault('acceptance_criteria',['完成指定任务并报告实际检查'])
            arguments.setdefault('change_scope',['仅指定工作区内的授权文件'])
        else:
            arguments.setdefault('repair_reason','按原条件修复已记录的问题')
        with patch.object(bridge.subprocess, "Popen", return_value=_FakeProc()) as popen, \
                patch.object(bridge, "ensure_server", side_effect=AssertionError("MCP 提交不得启动仪表盘")) as ensure:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                text, structured, is_error = self.tool("deepseek_submit", arguments)
            self.assertEqual(buffer.getvalue(), "", "bridge.submit 的输出必须被捕获，不能污染 MCP stdout")
        return text, structured, is_error, popen, ensure


class ProtocolTests(McpTestBase):
    def test_initialize_negotiates_supported_versions(self):
        for version in mcp_server.SUPPORTED_PROTOCOLS:
            response = self.request("initialize", {"protocolVersion": version, "clientInfo": {"name": "codex"}})
            self.assertEqual(response["result"]["protocolVersion"], version)
            self.assertIn("tools", response["result"]["capabilities"])
            self.assertEqual(response["result"]["serverInfo"]["name"], mcp_server.SERVER_NAME)
            self.assertTrue(response["result"]["instructions"])
        fallback = self.request("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(fallback["result"]["protocolVersion"], mcp_server.LATEST_PROTOCOL)

    def test_initialized_notification_and_ping(self):
        self.assertIsNone(mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.server))
        self.assertTrue(self.server.initialized)
        self.assertIsNone(mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/cancelled"}, self.server))
        self.assertIsNone(mcp_server.dispatch({"jsonrpc": "2.0", "method": "ping"}, self.server))
        self.assertEqual(self.request("ping")["result"], {})

    def test_tools_list_enumerates_ten_tools(self):
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [
            "deepseek_recovery", "deepseek_connection", "deepseek_return", "deepseek_handoff", "deepseek_submit", "deepseek_status", "deepseek_result",
            "deepseek_review", "deepseek_cancel", "deepseek_details"])
        by_name = {tool["name"]: tool for tool in tools}
        for name, tool in by_name.items():
            self.assertTrue(re.search(r"[\u4e00-\u9fff]", tool["description"]), name)
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        submit = by_name["deepseek_submit"]["inputSchema"]
        self.assertEqual(submit["properties"]["permission"]["enum"], ["read-only", "workspace-write"])
        self.assertEqual(submit["properties"]["permission"]["default"], "read-only")
        self.assertEqual(submit["properties"]["timeout_seconds"]["minimum"], 30)
        self.assertEqual(submit["properties"]["timeout_seconds"]["maximum"], 3600)
        self.assertNotIn("session_id", submit["properties"])
        self.assertIn('acceptance_criteria',submit['properties'])
        self.assertIn('change_scope',submit['properties'])
        self.assertIn('repair_reason',submit['properties'])
        status = by_name["deepseek_status"]["inputSchema"]["properties"]
        self.assertEqual(status["wait_seconds"]["maximum"], 45)
        result = by_name["deepseek_result"]["inputSchema"]["properties"]
        self.assertEqual(result["max_chars"]["maximum"], 16000)
        self.assertEqual(result["max_chars"]["default"], 8000)
        self.assertEqual(by_name["deepseek_review"]["inputSchema"]["properties"]["verdict"]["enum"],
                         ['accepted','changes-needed','inconclusive'])

    def test_protocol_errors_and_notifications_produce_no_prose(self):
        buffer = io.StringIO()
        mcp_server.process_line(b"{not json", buffer, self.server)
        payload = json.loads(buffer.getvalue().strip())
        self.assertEqual(payload["error"]["code"], mcp_server.PARSE_ERROR)
        self.assertIsNone(payload["id"])

        buffer = io.StringIO()
        mcp_server.process_line(json.dumps({"jsonrpc": "2.0", "method": "nope", "id": 7}).encode(), buffer, self.server)
        self.assertEqual(json.loads(buffer.getvalue())["error"]["code"], mcp_server.METHOD_NOT_FOUND)

        buffer = io.StringIO()
        mcp_server.process_line(json.dumps({"jsonrpc": "1.0", "method": "ping", "id": 1}).encode(), buffer, self.server)
        self.assertEqual(json.loads(buffer.getvalue())["error"]["code"], mcp_server.INVALID_REQUEST)

        buffer = io.StringIO()
        mcp_server.process_line(json.dumps([{"jsonrpc": "2.0", "method": "ping", "id": 1}]).encode(), buffer, self.server)
        self.assertEqual(json.loads(buffer.getvalue())["error"]["code"], mcp_server.INVALID_REQUEST)

        buffer = io.StringIO()
        mcp_server.process_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
                                buffer, self.server)
        self.assertEqual(buffer.getvalue(), "")

    def test_tool_call_error_shapes(self):
        response = self.request("tools/call", {"name": "deepseek_hack", "arguments": {}})
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)
        self.assertIn("deepseek_submit", response["error"]["data"]["available"])
        self.assertEqual(self.request("tools/call", {"arguments": {}})["error"]["code"], mcp_server.INVALID_PARAMS)
        self.assertEqual(
            self.request("tools/call", {"name": "deepseek_status", "arguments": "x"})["error"]["code"],
            mcp_server.INVALID_PARAMS)

    def test_subprocess_handshake_keeps_stdout_protocol_only(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ]
        payload = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
        process = subprocess.Popen([sys.executable, str(Path(mcp_server.__file__))], cwd=str(bridge.ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        out, err = process.communicate(payload, timeout=60)
        self.assertEqual(process.returncode, 0, err)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        responses = [json.loads(line) for line in lines]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(len(responses[2]["result"]["tools"]), 10)


class SubmitTests(McpTestBase):
    def test_official_computer_use_is_available_without_an_extra_submit_flag(self):
        with patch('desktop_client.check_connection', return_value={'ready': True, 'computer_use': True}):
            text, data, error, _, _ = self.submit_with_mocks({'workspace': str(self.workspace), 'task': 'GUI demo'})
        self.assertFalse(error, text); self.assertTrue(data['computer_use'])
        self.assertTrue(bridge.read_json(self.runs/data['task_id']/'state.json')['computer_use'])

    def test_absent_official_plugin_is_reported_honestly_without_blocking_other_delegated_work(self):
        text, data, error, _, _ = self.submit_with_mocks({'workspace': str(self.workspace), 'task': 'Read a file'})
        self.assertFalse(error, text); self.assertFalse(data['computer_use'])

    def test_unavailable_desktop_is_refused_before_allocating_task_or_worker(self):
        with patch('desktop_client.call',side_effect=RuntimeError('Harness 未连接')),patch.object(bridge.subprocess,'Popen') as spawn:
            text,data,error=self.tool('deepseek_submit',{'workspace':str(self.workspace),'task':'不应执行'})
        self.assertTrue(error);spawn.assert_not_called()
        self.assertIn('保持后台运行',text)
        self.assertEqual(list(self.runs.glob('*/state.json')),[])
        self.assertEqual(list(self.staging.glob('*')),[])

    def test_submit_defaults_confined_permission_and_no_dashboard(self):
        task = "中文任务：写文件 & echo A\n反引号 `code` 与 $(Get-Date)"
        started = time.monotonic()
        text, structured, is_error, popen, ensure = self.submit_with_mocks(
            {"workspace": str(self.workspace), "task": task})
        self.assertFalse(is_error, text)
        self.assertLess(time.monotonic() - started, 10)  # 立即返回，不等待模型
        ensure.assert_not_called()
        self.assertFalse(structured["dashboard_started"])
        self.assertFalse(structured["waited_for_model"])
        run_id = structured["task_id"]
        state = bridge.read_json(self.runs / run_id / "state.json")
        self.assertEqual(state["status"], "queued")
        self.assertEqual(state["permission"], "read-only")
        self.assertEqual(state["timeout_seconds"], 1200)
        self.assertEqual(state["profile"], bridge.PROFILE)
        self.assertEqual(state["model"], "deepseek-v4.1-flash")
        self.assertIsNone(state["resume_session"])
        self.assertNotIn("parent_task_id", state)
        self.assertIn("?run=" + run_id, state["dashboard"])
        self.assertEqual((self.runs / run_id / "task.txt").read_text(encoding="utf-8"), task)
        self.assertEqual(structured["title"], task.splitlines()[0])
        self.assertEqual(structured["status"], "queued")
        self.assertEqual(popen.call_args[0][0][1:], [str(bridge.ROOT / "bridge.py"), "worker", run_id])
        self.assertEqual(list(self.staging.glob("*.txt")), [])

    def test_submit_workspace_write_and_custom_timeout(self):
        text, structured, is_error, _, _ = self.submit_with_mocks(
            {"workspace": str(self.workspace), "task": "写入文件", "title": "写任务",
             "permission": "workspace-write", "timeout_seconds": 60})
        self.assertFalse(is_error, text)
        state = bridge.read_json(self.runs / structured["task_id"] / "state.json")
        self.assertEqual(state["permission"], "workspace-write")
        self.assertEqual(state["timeout_seconds"], 60)
        self.assertEqual(state["title"], "写任务")

    def test_submit_rejects_invalid_arguments(self):
        cases = [
            ({"workspace": str(self.workspace), "task": " "}, "task"),
            ({"workspace": "relative/path", "task": "x"}, "绝对路径"),
            ({"workspace": str(self.root / "missing"), "task": "x"}, "workspace"),
            ({"workspace": str(self.dsh), "task": "x"}, "目录"),
            ({"task": "x"}, "workspace"),
            ({"workspace": str(self.workspace), "task": "x", "permission": "root"}, "permission"),
            ({"workspace": str(self.workspace), "task": "x", "timeout_seconds": 29}, "timeout_seconds"),
            ({"workspace": str(self.workspace), "task": "x", "timeout_seconds": 3601}, "timeout_seconds"),
            ({"workspace": str(self.workspace), "task": "x", "timeout_seconds": "60"}, "timeout_seconds"),
            ({"workspace": str(self.workspace), "task": "x", "title": 5}, "title"),
            ({"workspace": str(self.workspace), "task": "x", "extra": 1}, "不支持的参数"),
            ({"workspace": str(self.workspace), "task": "x", "session_id": SESSION}, "session_id"),
        ]
        for arguments, needle in cases:
            with self.subTest(arguments=arguments):
                text, _, is_error = self.tool("deepseek_submit", arguments)
                self.assertTrue(is_error, text)
                self.assertIn(needle, text)
                self.assertFalse((self.runs / RUN_ID).exists())


class ContinuationTests(McpTestBase):
    def parent(self, **overrides):
        state = {"status": "completed", "permission": "read-only", "profile": bridge.PROFILE,
                 "workspace": str(self.workspace), "session_id": SESSION, 'backend':'desktop'}
        state.update(overrides)
        path = self.run_dir(OTHER_ID, state=state)
        value = bridge.read_json(path/'state.json')
        plan = bridge.policy.prepare(self.runs,None,{'acceptance_criteria':['满足原任务要求'],'change_scope':['指定文件']},None)
        bridge.policy.reserve(plan,path,value,bridge.write_json)
        bridge.write_json(path/'state.json',value)
        bridge.write_json(path/'review.json',{'task_id':OTHER_ID,'contract_hash':value['contract_hash'],'verdict':'changes-needed',
            'checks':[{'criterion_id':'C1','outcome':'failed','evidence':'实际结果尚未满足原任务要求'}],'suggestions':[]})
        return path

    def rejected(self, overrides, permission=None, needle="拒绝"):
        self.parent(**overrides)
        arguments = {"workspace": str(self.workspace), "task": "继续", "parent_task_id": OTHER_ID}
        if permission:
            arguments["permission"] = permission
        with patch.object(bridge, "ensure_server", side_effect=AssertionError("no dashboard")):
            text, _, is_error = self.tool("deepseek_submit", arguments)
        self.assertTrue(is_error, text)
        self.assertIn(needle, text)
        self.assertTrue((self.runs / OTHER_ID).exists())

    def test_continuation_reuses_parent_session_and_records_reference(self):
        self.parent()
        text, structured, is_error, _, _ = self.submit_with_mocks(
            {"workspace": str(self.workspace), "task": "继续任务", "parent_task_id": OTHER_ID})
        self.assertFalse(is_error, text)
        self.assertTrue(structured["session_reused"])
        self.assertEqual(structured["parent_task_id"], OTHER_ID)
        run_id = structured["task_id"]
        state = bridge.read_json(self.runs / run_id / "state.json")
        self.assertEqual(state["resume_session"], SESSION)
        self.assertEqual(state["parent_task_id"], OTHER_ID)
        events = (self.runs / run_id / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("continuation", events)
        self.assertIn(OTHER_ID, events)

    def test_running_parent_is_refused(self):
        self.rejected({"status": "running"}, needle="仍在运行")

    def test_workspace_mismatch_is_refused(self):
        self.rejected({"workspace": str(self.root)}, needle="工作区")

    def test_foreign_profile_is_refused(self):
        self.rejected({"profile": "someone-else"}, needle="桥接配置")

    def test_missing_or_malformed_session_is_refused(self):
        self.rejected({"session_id": None}, needle="会话")
        self.parent(session_id="session-not-a-uuid & echo x")
        text, _, is_error = self.tool("deepseek_submit",
            {"workspace": str(self.workspace), "task": "继续", "parent_task_id": OTHER_ID})
        self.assertTrue(is_error, text)
        self.assertIn("格式异常", text)

    def test_unknown_parent_is_refused(self):
        text, _, is_error = self.tool("deepseek_submit",
            {"workspace": str(self.workspace), "task": "继续", "parent_task_id": "20260101-000000-00000000"})
        self.assertTrue(is_error, text)
        self.assertIn("无效或不存在", text)

    def test_permission_escalation_is_refused(self):
        self.rejected({}, permission="workspace-write", needle="提升权限")

    def test_unknown_parent_permission_is_refused(self):
        self.rejected({"permission": None}, needle="权限记录异常")

    def test_equal_permission_continuation_is_allowed(self):
        self.parent(permission="workspace-write")
        text, structured, is_error, _, _ = self.submit_with_mocks(
            {"workspace": str(self.workspace), "task": "继续", "parent_task_id": OTHER_ID,
             "permission": "workspace-write"})
        self.assertFalse(is_error, text)
        state = bridge.read_json(self.runs / structured["task_id"] / "state.json")
        self.assertEqual(state["permission"], "workspace-write")


class StatusCursorTests(McpTestBase):
    def test_initial_tail_then_forward_events_with_partial_line(self):
        events = [{"type": "tool_call", "callId": f"c{i}", "tool": "read",
                   "input": {"file": f"f{i}.txt"}, "order": i} for i in range(1, 21)]
        path = self.run_dir(state={"status": "running"}, events=events)
        text, first = self.status({"task_id": RUN_ID})
        self.assertEqual(first["events_returned"], 12)
        self.assertIn("f9.txt", first["events"][0]["text"])
        self.assertIn("f20.txt", first["events"][-1]["text"])
        self.assertTrue(first["cursor"])

        with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "tool_result", "callId": "c99", "status": "completed",
                                     "result": "ok-99", "order": 21}, ensure_ascii=False) + "\n")
            handle.write(json.dumps({"type": "final", "text": "半行结果", "order": 22}, ensure_ascii=False))
        text2, second = self.status({"task_id": RUN_ID, "cursor": first["cursor"]})
        self.assertEqual(second["events_returned"], 1)
        self.assertIn("ok-99", second["events"][0]["text"])
        self.assertNotIn("半行结果", text2)  # 未写完的尾行不得被跳过或误读

        with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        text3, third = self.status({"task_id": RUN_ID, "cursor": second["cursor"]})
        self.assertEqual(third["events_returned"], 1)
        self.assertEqual(third["events"][0]["kind"], "final")
        seen = [item["text"] for item in second["events"] + third["events"]]
        self.assertEqual(len(seen), len(set(seen)))

    def test_cursor_pagination_under_cap_loses_nothing(self):
        path = self.run_dir(state={"status": "running"}, events=[{"type": "status", "phase": "turn_start", "order": 0}])
        _, first = self.status({"task_id": RUN_ID})
        cursor = first["cursor"]
        with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
            for index in range(1, 31):
                handle.write(json.dumps({"type": "tool_call", "callId": f"p{index}", "tool": "read",
                                         "input": {"file": f"x{index}"}, "order": index}, ensure_ascii=False) + "\n")
        batches = []
        for _ in range(5):
            _, structured = self.status({"task_id": RUN_ID, "cursor": cursor})
            cursor = structured["cursor"]
            batches.append(structured)
            if not structured["events"]:
                break
        delivered = [item["text"] for batch in batches for item in batch["events"]]
        self.assertEqual(len(delivered), 30, batches)
        self.assertEqual(len(delivered), len(set(delivered)), "增量读取不得重复")
        for index in range(1, 31):
            self.assertTrue(any(f"x{index}" in item for item in delivered), f"缺少事件 x{index}")
        self.assertGreaterEqual(batches[0]["events_returned"], 12)

    def test_compact_events_are_bounded_scrubbed_and_hide_thinking(self):
        long_text = "A" * 1000
        events = [
            {"type": "thinking", "text": "PRIVATE_REASONING", "order": 1},
            {"type": "session", "sessionId": "session-ignored", "order": 2},
            {"type": "tool_call", "callId": "t1", "tool": "shell", "input": {"command": "echo $HOME"}, "order": 3},
            {"type": "tool_result", "callId": "t1", "status": "completed", "result": long_text, "order": 4},
            {"type": "status", "phase": "step_end", "usage": {"inputTokens": 3}, "order": 5},
            {"type": "bridge_status", "status": "completed", "order": 6},
        ]
        notes = [{"type": "codex", "text": "验收：api_key=ABCD1234EFGH 需要修复", "order": 7}]
        self.run_dir(events=events, notes=notes,
                     state={"status": "completed", "review": "changes-needed", "error": "sk-abcdefghijklmnop"})
        text, structured = self.status({"task_id": RUN_ID})
        self.assertNotIn("PRIVATE_REASONING", text)
        self.assertNotIn("PRIVATE_REASONING", json.dumps(structured, ensure_ascii=False))
        self.assertNotIn("sk-abcdefghijklmnop", text)
        self.assertNotIn("ABCD1234EFGH", text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("调用工具 shell", text)
        self.assertIn("工具 shell 结果", text)
        self.assertIn("已完成步骤：1", text)
        self.assertIn("changes-needed", text)
        self.assertIn("Codex 批注", text)
        self.assertIn("completed", text)
        for item in structured["events"]:
            self.assertLessEqual(len(item["text"]), 400)
        self.assertFalse(structured["steps_partial"] is None)

    def test_cursor_rejects_cross_task_and_corrupt_values(self):
        self.run_dir(RUN_ID, events=[{"type": "status", "phase": "turn_start", "order": 1}])
        self.run_dir(OTHER_ID, events=[{"type": "status", "phase": "turn_start", "order": 1}])
        _, first = self.status({"task_id": RUN_ID})
        cursor = first["cursor"]
        self.assertNotIn(RUN_ID, cursor)

        text, _, is_error = self.tool("deepseek_status", {"task_id": OTHER_ID, "cursor": cursor})
        self.assertTrue(is_error)
        self.assertIn("不匹配", text)

        for bad in ["not-a-cursor", "!!!", "e30", cursor[:-4] + "zzzz"]:
            text, _, is_error = self.tool("deepseek_status", {"task_id": RUN_ID, "cursor": bad})
            self.assertTrue(is_error, bad)
            self.assertIn("游标", text)

    def test_forged_cursor_offset_beyond_eof_is_clamped_not_crashing(self):
        self.run_dir(RUN_ID, events=[{"type": "status", "phase": "turn_start", "order": 1}])
        payload = {"v": mcp_server.CURSOR_VERSION, "task": RUN_ID,
                   "files": {name: {"offset": 10 ** 9} for name in mcp_server.LOG_FILES},
                   "steps": 0, "partial": True}
        cursor = mcp_server.encode_cursor(payload)
        text, _, is_error = self.tool("deepseek_status", {"task_id": RUN_ID, "cursor": cursor})
        self.assertFalse(is_error, text)
        self.assertIn("轮转", text)

    def test_wait_seconds_and_max_events_bounds(self):
        self.run_dir()
        for bad in (-1, 46, "5", True):
            text, _, is_error = self.tool("deepseek_status", {"task_id": RUN_ID, "wait_seconds": bad})
            self.assertTrue(is_error, text)
            self.assertIn("wait_seconds", text)
        for bad in (0, 13, 1.5):
            text, _, is_error = self.tool("deepseek_status", {"task_id": RUN_ID, "max_events": bad})
            self.assertTrue(is_error, text)
            self.assertIn("max_events", text)
        text, _, is_error = self.tool("deepseek_status", {"task_id": RUN_ID, "wait_seconds": 0, "max_events": 12})
        self.assertFalse(is_error, text)

    def test_wait_returns_immediately_when_task_stopped(self):
        self.run_dir(state={"status": "completed"})
        started = time.monotonic()
        text, structured = self.status({"task_id": RUN_ID, "wait_seconds": 45})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(structured["events_returned"], 0)
        self.assertIn("暂无新事件", text)


class ResultReviewCancelTests(McpTestBase):
    def test_result_truncation_and_defaults(self):
        content = "中文结果" * 5000
        self.run_dir(result=content)
        text, structured, is_error = self.tool("deepseek_result", {"task_id": RUN_ID})
        self.assertFalse(is_error, text)
        self.assertEqual(structured["total_chars"], 20000)
        self.assertEqual(structured["max_chars"], 8000)
        self.assertTrue(structured["truncated"])
        self.assertEqual(len(structured["content"]), 8000)
        self.assertIn("已截断", text)

    def test_result_custom_bound_and_validation(self):
        self.run_dir(result="x" * 100)
        text, structured, is_error = self.tool("deepseek_result", {"task_id": RUN_ID, "max_chars": 20})
        self.assertFalse(is_error, text)
        self.assertEqual(len(structured["content"]), 20)
        self.assertTrue(structured["truncated"])
        for bad in (0, 16001, "many", True):
            text, _, is_error = self.tool("deepseek_result", {"task_id": RUN_ID, "max_chars": bad})
            self.assertTrue(is_error, text)
            self.assertIn("max_chars", text)

    def test_result_reports_failure_and_missing_result(self):
        self.run_dir(RUN_ID, state={"status": "failed", "error": "worker 挂了"}, result="部分输出")
        text, structured, is_error = self.tool("deepseek_result", {"task_id": RUN_ID})
        self.assertTrue(is_error)
        self.assertEqual(structured["status"], "failed")
        self.assertIn("部分输出", text)
        self.assertIn("worker 挂了", text)

        self.run_dir(OTHER_ID, state={"status": "running"})
        text, structured, is_error = self.tool("deepseek_result", {"task_id": OTHER_ID})
        self.assertTrue(is_error)
        self.assertFalse(structured["available"])
        self.assertIn("尚未产生最终结果", text)

    def test_review_requires_stopped_task_and_persists_verdict(self):
        self.run_dir(state={"status": "running"})
        text, _, is_error = self.tool("deepseek_review",
            {"task_id": RUN_ID, "text": "看起来不错", "verdict": "accepted"})
        self.assertTrue(is_error)
        self.assertIn("仍在运行", text)
        state = bridge.read_json(self.runs / RUN_ID / "state.json")
        self.assertNotIn("review", state)
        self.assertFalse((self.runs / RUN_ID / "notes.jsonl").exists())

        bridge.write_json(self.runs / RUN_ID / "state.json", {**state, "status": "completed"})
        text, structured, is_error = self.tool("deepseek_review",
            {"task_id": RUN_ID, "text": "验收：`code` 与 $(x) 保留", "verdict": "accepted"})
        self.assertFalse(is_error, text)
        self.assertEqual(structured["verdict"], "accepted")
        self.assertEqual(bridge.read_json(self.runs / RUN_ID / "state.json")["review"], "accepted")
        notes = (self.runs / RUN_ID / "notes.jsonl").read_text(encoding="utf-8")
        self.assertIn("$(x)", notes)
        self.assertIn("accepted", text)

        for arguments, needle in (
                ({"task_id": RUN_ID, "text": "x", "verdict": "maybe"}, "verdict"),
                ({"task_id": RUN_ID, "text": " ", "verdict": "accepted"}, "text")):
            text, _, is_error = self.tool("deepseek_review", arguments)
            self.assertTrue(is_error, text)
            self.assertIn(needle, text)

    def test_cancel_active_writes_flag_only_and_stopped_is_unchanged(self):
        path = self.run_dir(state={"status": "running"})
        before = (path / "state.json").read_bytes()
        text, structured, is_error = self.tool("deepseek_cancel", {"task_id": RUN_ID})
        self.assertFalse(is_error, text)
        self.assertTrue(structured["cancel_requested"])
        self.assertTrue((path / "cancel.request").exists())
        self.assertEqual(before, (path / "state.json").read_bytes())
        self.assertIn("running", text)

        path2 = self.run_dir(OTHER_ID, state={"status": "completed"})
        before2 = (path2 / "state.json").read_bytes()
        text, structured, is_error = self.tool("deepseek_cancel", {"task_id": OTHER_ID})
        self.assertFalse(is_error, text)
        self.assertFalse(structured["cancel_requested"])
        self.assertFalse((path2 / "cancel.request").exists())
        self.assertEqual(before2, (path2 / "state.json").read_bytes())
        self.assertIn("已停止", text)

    def test_details_starts_dashboard_and_never_opens_browser(self):
        self.run_dir(result="done")
        with patch.object(bridge, "ensure_server", return_value="http://127.0.0.1:47831") as ensure:
            text, structured, is_error = self.tool("deepseek_details", {"task_id": RUN_ID})
        self.assertFalse(is_error, text)
        ensure.assert_called_once()
        self.assertTrue(structured["dashboard_started"])
        self.assertEqual(structured["dashboard"], f"http://127.0.0.1:47831/?run={RUN_ID}")
        self.assertFalse(structured["browser_opened"])
        self.assertIsNotNone(structured["file_sizes"]["result.md"])

    def test_details_survives_dashboard_failure(self):
        self.run_dir()
        with patch.object(bridge, "ensure_server", side_effect=RuntimeError("端口占用")):
            text, structured, is_error = self.tool("deepseek_details", {"task_id": RUN_ID})
        self.assertFalse(is_error, text)
        self.assertFalse(structured["dashboard_started"])
        self.assertIn("端口占用", text)
        self.assertEqual(structured["dashboard"], mcp_server._dashboard_url(RUN_ID))

    def test_task_id_traversal_is_rejected_everywhere(self):
        self.run_dir()
        for bad in ("../../private", "..\\..\\private", "20261005-120000-1234ABCD", "unknown-task", "run/../../x"):
            for tool_name in ("deepseek_status", "deepseek_result", "deepseek_review",
                              "deepseek_cancel", "deepseek_details"):
                arguments = {"task_id": bad}
                if tool_name == "deepseek_review":
                    arguments.update({"text": "x", "verdict": "accepted"})
                with self.subTest(tool=tool_name, task_id=bad):
                    text, _, is_error = self.tool(tool_name, arguments)
                    self.assertTrue(is_error, text)


class ModuleHygieneTests(McpTestBase):
    def test_adapter_has_no_browser_or_extra_listener(self):
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        for needle in ("webbrowser", "startfile", "http.server", "ThreadingHTTPServer", "socketserver", "socket("):
            self.assertNotIn(needle, source, needle)
        self.assertEqual(source.count("bridge.ensure_server()"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
