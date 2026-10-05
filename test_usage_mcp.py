"""Ensure metering is visible through actual read-only MCP entrypoints, no models."""
import json
from test_mcp import McpTestBase, RUN_ID
import mcp_server


class UsageMcpTests(McpTestBase):
    def test_status_and_result_metering_are_visible_without_dispatch(self):
        (self.root/'settings.json').write_text(json.dumps({'backend':'desktop','usage_plan':'go'}),encoding='utf-8')
        path=self.run_dir(state={'backend':'desktop'}, result='完成',events=[
            {'type':'usage_capture','version':1,'order':1},
            {'type':'usage','seq':10,'order':2,'started':'2026-10-05T04:10:00Z','time':'2026-10-05T04:11:00Z',
             'usage':{'inputTokens':1000,'outputTokens':200,'cacheReadTokens':9000}},
            {'type':'status','phase':'step_end','order':3}])
        before=(path/'events.jsonl').read_bytes()
        for fn in (mcp_server.tool_status,mcp_server.tool_result):
            text, data, error=fn({'task_id':RUN_ID})
            self.assertFalse(error)
            self.assertIn('未缓存输入 1000',text)
            self.assertIn('非额外扣款',text)
            self.assertAlmostEqual(data['metering']['cost']['usd_min'],.000297)
            self.assertEqual(data['metering']['quota_contribution']['plan'],'go')
        self.assertEqual(before,(path/'events.jsonl').read_bytes())

    def test_old_native_missing_usage_is_not_displayed_as_zero_price(self):
        self.run_dir(state={'backend':'desktop'},result='旧结果')
        text,data,error=mcp_server.tool_result({'task_id':RUN_ID})
        self.assertFalse(error)
        self.assertIn('用量：未知',text)
        self.assertIsNone(data['metering']['cost']['usd_min'])
