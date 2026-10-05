"""Desktop worker lifecycle checks; no models, tokens, or real host calls."""
import json
import datetime as dt
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bridge
import desktop_client
import mcp_server

TASK = '20261005-120000-1234abcd'
SESSION = 'session-11111111-1111-1111-1111-111111111111'

class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-desktop-test-')
        self.root = Path(self.temp.name); self.runs = self.root/'runs'; self.run = self.runs/TASK
        self.run.mkdir(parents=True); (self.run/'task.txt').write_text('中文只读任务', encoding='utf8')
        (self.root/'worker-instructions.txt').write_text('Scoped task.', encoding='utf8')
        (self.root/'worker-reminder.txt').write_text('继续遵守本会话规则。', encoding='utf8')
        self.state = {'id':TASK,'workspace':str(self.root),'permission':'read-only','title':'中文',
            'backend':'desktop','status':'queued','created':bridge.stamp(),'timeout_seconds':60,'profile':bridge.PROFILE}
        bridge.write_json(self.run/'state.json',self.state)
        self.patches = [patch.object(bridge,'ROOT',self.root),patch.object(bridge,'RUNS',self.runs)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def native(self,status='completed',control='codex'):
        return {'status':status,'control':control,'owner':control,'session_id':SESSION,
            'workspace_id':'workspace-1','workspace':str(self.root),'desktop_title':'[Codex] 中文','desktop_visible':True}
    def test_worker_records_native_session_without_starting_harness_process(self):
        with patch.object(desktop_client,'call',return_value=self.native()) as call, patch.object(bridge.subprocess,'Popen') as spawn:
            bridge.worker(TASK)
        spawn.assert_not_called()
        state=bridge.read_json(self.run/'state.json');self.assertEqual(state['status'],'completed')
        self.assertEqual(state['session_id'],SESSION)
        sent=call.call_args.kwargs['task'];self.assertIn('中文只读任务',sent)
    def test_worker_observes_takeover_and_never_cancels_human_work(self):
        (self.run/'cancel.request').touch()
        with patch.object(desktop_client,'call',return_value=self.native('handed_off','human')) as call:
            bridge.worker(TASK)
        self.assertEqual(call.call_count,1)
        self.assertEqual(bridge.read_json(self.run/'state.json')['control'],'human')
    def test_cancellation_race_keeps_handoff_result(self):
        (self.run/'cancel.request').touch()
        with patch.object(desktop_client,'call',side_effect=[self.native('running'),RuntimeError('用户已接手'),self.native('handed_off','human')]):
            bridge.worker(TASK)
        self.assertEqual(bridge.read_json(self.run/'state.json')['status'],'handed_off')
    def test_disconnect_fails_without_resending(self):
        with patch.object(desktop_client,'call',side_effect=RuntimeError('host disconnected')) as call:
            bridge.worker(TASK)
        self.assertEqual(call.call_count,1)
        self.assertEqual(bridge.read_json(self.run/'state.json')['status'],'failed')
    def test_finished_task_reflects_later_client_takeover(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION})
        with patch.object(desktop_client,'call',return_value=self.native('completed','human')):
            state=bridge.current_state(self.run)
        self.assertEqual(state['control'],'human')
        with patch.object(desktop_client,'call',return_value=self.native('completed','human')):
            with self.assertRaises(mcp_server.ToolError):mcp_server._resolve_continuation(TASK,self.root,'read-only')
    def test_discovery_rejects_nonlocal_endpoint_and_malformed_capability(self):
        with patch.object(desktop_client,'STATE',self.root):
            (self.root/'connection.json').write_text(json.dumps({'protocol':desktop_client.PROTOCOL,'port':'https://remote','token':'invalid'}))
            with self.assertRaisesRegex(RuntimeError,'Invalid desktop'):desktop_client.call('health')

    def test_handoff_includes_bounded_report_and_latest_review_without_model_call(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION,'review':'changes-needed'})
        (self.run/'result.md').write_text('已修改 app.py\n' + '长报告'*900,encoding='utf8')
        bridge.event(self.run,{'type':'codex','verdict':'accepted','text':'旧批注'},'notes.jsonl')
        bridge.event(self.run,{'type':'codex','verdict':'changes-needed','text':'需要补查失败路径'},'notes.jsonl')
        with patch.object(desktop_client,'call',return_value=self.native('completed','human')) as call:
            text,data,error=mcp_server.tool_handoff({'task_id':TASK})
        self.assertFalse(error)
        self.assertTrue(all(c.args[0] in ('status','handoff') for c in call.call_args_list))
        self.assertIn('需要补查失败路径',text)
        self.assertNotIn('旧批注',text)
        self.assertIn('报告已截断',text)
        self.assertIn('read-only',text)
        self.assertEqual(data['control'],'human')
        self.assertEqual(Path(data['handoff_file']).read_text(encoding='utf8').strip(),data['handoff_summary'])

    def test_handoff_brief_missing_result_does_not_claim_success(self):
        brief=mcp_server._handoff_brief(self.run,{**self.state,'status':'handed_off'})
        self.assertIn('尚无可读取的最终报告',brief)
        self.assertIn('未验收',brief)

    def test_native_tool_progress_displays_file_and_result_without_thinking(self):
        calls={}
        item=mcp_server._compact({'type':'tool_call','name':'read','call_id':'c1',
                                  'arguments':'{"file":"app.py"}'},'events.jsonl',calls)
        self.assertIn('app.py',item['text'])
        result=mcp_server._compact({'type':'tool_result','call_id':'c1','is_error':True,
                                    'content':[{'type':'thinking','text':'HIDDEN'},{'type':'text','text':'找不到文件'}]},'events.jsonl',calls)
        self.assertIn('找不到文件',result['text']);self.assertIn('失败',result['text'])
        self.assertNotIn('HIDDEN',result['text'])

    def test_return_control_never_starts_a_model_and_preserves_session(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION,'control':'human'})
        def respond(operation, **kwargs):
            if operation == 'health': return {'return_control':True}
            return self.native('completed','human' if operation == 'status' else 'codex')
        with patch.object(desktop_client,'call',side_effect=respond) as call:
            text,data,error=mcp_server.tool_return({'task_id':TASK})
        self.assertFalse(error);self.assertFalse(data['model_task_started'])
        self.assertEqual(data['session_id'],SESSION)
        self.assertEqual([c.args[0] for c in call.call_args_list],['status','health','return'])
        self.assertEqual(bridge.read_json(self.run/'state.json')['control'],'codex')

    def test_return_rejects_old_connector_and_busy_client(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION,'control':'human'})
        with patch.object(desktop_client,'call',side_effect=[self.native('completed','human'),{}]) as call:
            with self.assertRaisesRegex(mcp_server.ToolError,'重开 Harness'):
                mcp_server.tool_return({'task_id':TASK})
        self.assertEqual(call.call_count,2)
        with patch.object(desktop_client,'call',side_effect=[self.native('completed','human'),{'return_control':True},RuntimeError('客户端仍在运行')]):
            with self.assertRaisesRegex(RuntimeError,'仍在运行'):
                mcp_server.tool_return({'task_id':TASK})
        self.assertEqual(bridge.read_json(self.run/'state.json')['control'],'human')

    def test_first_prompt_full_then_same_session_brief_and_task_literal_preserved(self):
        rules='完整规则。'*200
        (self.root/'worker-instructions.txt').write_text(rules,encoding='utf8')
        with patch.object(desktop_client,'call',return_value=self.native()) as first:
            bridge.worker(TASK)
        self.assertIn(rules,first.call_args.kwargs['task'])
        child_id='20261005-120001-1234abcd'; child=self.runs/child_id;child.mkdir()
        task='继续修改 "中文" `literal` $(keep)\n第二行'
        (child/'task.txt').write_text(task,encoding='utf8')
        bridge.write_json(child/'state.json',{**self.state,'id':child_id,'parent_task_id':TASK,'resume_session':SESSION})
        with patch.object(desktop_client,'call',return_value=self.native()) as second:
            bridge.worker(child_id)
        sent=second.call_args.kwargs['task']
        self.assertNotIn(rules,sent);self.assertIn('继续遵守本会话规则',sent)
        self.assertTrue(sent.endswith(task));self.assertLess(len(sent),len(first.call_args.kwargs['task']))
        self.assertEqual(bridge.read_json(child/'state.json')['prompt_mode'],'brief')

    def test_full_rules_resent_when_policy_changes_or_prior_delivery_is_uncertain(self):
        with patch.object(desktop_client,'call',return_value=self.native()):bridge.worker(TASK)
        parent=bridge.read_json(self.run/'state.json')
        child_id='20261005-120001-1234abcd';child=self.runs/child_id;child.mkdir()
        (child/'task.txt').write_text('继续',encoding='utf8')
        child_state={**self.state,'id':child_id,'parent_task_id':TASK,'resume_session':SESSION}
        variants=[{'status':'failed'},{'prompt_policy_hash':None},{'session_id':'other'},
                  {'backend':'headless'},{'workspace':str(self.root/'other')}]
        for change in variants:
            with self.subTest(change=change):
                bridge.write_json(self.run/'state.json',{**parent,**change})
                state=dict(child_state);sent=bridge.desktop_prompt(child,state)
                self.assertEqual(state['prompt_mode'],'full');self.assertIn('Scoped task.',sent)
        bridge.write_json(self.run/'state.json',parent)
        (self.root/'worker-instructions.txt').write_text('UPDATED RULES',encoding='utf8')
        state=dict(child_state);sent=bridge.desktop_prompt(child,state)
        self.assertEqual(state['prompt_mode'],'full');self.assertIn('UPDATED RULES',sent)

    def test_stage_separates_execution_acceptance_and_human_ownership(self):
        cases=[({'status':'completed'},'awaiting_review'),
               ({'status':'completed','review':'accepted'},'accepted'),
               ({'status':'completed','review':'changes-needed'},'needs_attention'),
               ({'status':'completed','control':'human'},'human_control'),
               ({'status':'completed','returned_to_codex':'time'},'returned_to_codex'),
               ({'status':'running','returned_to_codex':'time'},'running'),
               ({'status':'completed','control':'human','desktop_connection':'unavailable'},'connection_unavailable')]
        for state,stage in cases:
            with self.subTest(stage=stage):
                view=bridge.task_view(state)
                self.assertEqual(view['stage'],stage)
                self.assertTrue(view['next_action']);self.assertTrue(view['control_label'])

    def test_status_tool_exposes_current_stage_and_keeps_raw_status(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION})
        with patch.object(desktop_client,'call',return_value=self.native('completed','human')):
            text,data,error=mcp_server.tool_status({'task_id':TASK})
        self.assertFalse(error);self.assertEqual(data['status'],'completed')
        self.assertEqual(data['stage'],'human_control');self.assertIn('由谁负责：用户',text)
        self.assertIn('下一步',text)
        with patch.object(desktop_client,'call',side_effect=RuntimeError('closed')):
            text,data,error=mcp_server.tool_status({'task_id':TASK})
        self.assertEqual(data['stage'],'connection_unavailable');self.assertIn('历史状态',text)

    def test_connection_check_ready_is_read_only_and_does_not_claim_model_access(self):
        health={'protocol':desktop_client.PROTOCOL,'version':1,'revision':5,'native_session':True,'return_control':True}
        before=set(self.runs.rglob('*'))
        with patch.object(desktop_client,'call',return_value=health) as call,patch.object(bridge.subprocess,'Popen') as spawn:
            text,data,error=mcp_server.tool_connection({})
        self.assertFalse(error);self.assertTrue(data['ready'])
        self.assertFalse(data['model_task_started']);self.assertFalse(data['model_access_verified'])
        self.assertFalse(data['client_opened']);spawn.assert_not_called()
        call.assert_called_once_with('health',timeout=3)
        self.assertEqual(set(self.runs.rglob('*')),before)
        self.assertTrue(mcp_server.TOOLS['deepseek_connection']['annotations']['readOnlyHint'])

    def test_connection_check_unavailable_provides_recovery_and_scrubs_diagnostics(self):
        with patch.object(desktop_client,'call',side_effect=RuntimeError('连接断开 Bearer FAKE_TOKEN_FOR_TEST')):
            text,data,error=mcp_server.tool_connection({})
        self.assertFalse(error);self.assertEqual(data['connection_state'],'unavailable')
        self.assertFalse(data['ready']);self.assertIn('保持后台运行',text)
        self.assertNotIn('FAKE_TOKEN_FOR_TEST',text)
        with self.assertRaises(mcp_server.ToolError):mcp_server.tool_connection({'task':'不要执行'})

    def test_connection_check_distinguishes_old_and_incompatible_connectors(self):
        cases=[({'protocol':desktop_client.PROTOCOL,'version':1,'revision':4,'native_session':True},'upgrade_needed'),
               ({'protocol':'different','version':1},'incompatible'),
               (['invalid'],'incompatible')]
        for health,expected in cases:
            with self.subTest(expected=expected):
                with patch.object(desktop_client,'call',return_value=health):
                    info=desktop_client.check_connection()
                self.assertEqual(info['connection_state'],expected)
                self.assertFalse(info['ready']);self.assertTrue(info['connected'])

    def interrupted_state(self,**extra):
        state={**self.state,'status':'running','worker_pid':99999999,
               'created':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=2)).isoformat(),**extra}
        bridge.write_json(self.run/'state.json',state)
        return state

    def test_dead_monitor_does_not_mark_running_native_task_failed_or_restart_it(self):
        self.interrupted_state()
        with patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',return_value=self.native('running')) as call,patch.object(bridge.subprocess,'Popen') as spawn:
            state=bridge.current_state(self.run)
        self.assertEqual(state['status'],'running');self.assertTrue(state['monitor_detached'])
        self.assertFalse(state['execution_uncertain']);spawn.assert_not_called()
        call.assert_called_once_with('status',timeout=2,task_id=TASK)
        self.assertEqual(bridge.read_json(self.run/'state.json')['status'],'running')

    def test_dead_monitor_offline_remains_uncertain_and_blocks_duplicate_submission(self):
        self.interrupted_state()
        launcher=self.root/'fake.cmd';launcher.touch()
        args=SimpleNamespace(workspace=str(self.root),task_file=str(self.run/'task.txt'),backend='desktop',
                             no_dashboard=True,title='不要重做',permission='read-only',timeout=60)
        with (patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',side_effect=RuntimeError('offline')),
              patch.object(desktop_client,'check_connection',return_value={'ready':True}),patch.object(bridge,'DSH',launcher),patch.object(bridge.subprocess,'Popen') as spawn):
            state=bridge.current_state(self.run)
            with self.assertRaisesRegex(RuntimeError,'overlaps'):bridge.submit(args)
        self.assertEqual(state['stage'],'execution_uncertain');self.assertEqual(state['status'],'running')
        spawn.assert_not_called();self.assertEqual(len(list(self.runs.glob('*/state.json'))),1)

    def test_failed_monitor_without_session_id_reconciles_completed_native_result(self):
        self.interrupted_state(status='failed',error='connection lost')
        (self.run/'result.md').write_text('已有结果，不重做',encoding='utf8')
        with patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',return_value=self.native()):
            text,data,error=mcp_server.tool_recovery({'task_id':TASK})
        self.assertFalse(error);self.assertEqual(data['decision'],'inspect_original')
        self.assertEqual(data['status'],'completed');self.assertFalse(data['continuation_candidate'])
        self.assertTrue(data['result_available']);self.assertFalse(data['model_task_started'])
        self.assertEqual(bridge.read_json(self.run/'state.json')['status'],'completed')

    def test_recovery_uses_bounded_public_progress_and_does_not_claim_verified_changes(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION})
        for i in range(12):bridge.event(self.run,{'type':'text','text':'进度'+str(i)})
        bridge.event(self.run,{'type':'thinking','text':'PRIVATE_THOUGHT'})
        (self.run/'result.md').write_text('模型报告：已修改 app.py',encoding='utf8')
        with patch.object(desktop_client,'call',return_value=self.native()) as call:
            text,data,error=mcp_server.tool_recovery({'task_id':TASK})
        self.assertLessEqual(len(data['progress']),6);self.assertIn('进度11',text)
        self.assertNotIn('PRIVATE_THOUGHT',text);self.assertFalse(data['files_independently_verified'])
        self.assertEqual(call.call_count,1);self.assertEqual(call.call_args.args[0],'status')

    def test_recovery_never_reclaims_human_session_and_rejects_old_parent(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','session_id':SESSION})
        with patch.object(desktop_client,'call',return_value=self.native('completed','human')) as call:
            _,data,_=mcp_server.tool_recovery({'task_id':TASK})
        self.assertEqual(data['decision'],'human_control');self.assertFalse(data['continuation_candidate'])
        self.assertEqual(call.call_count,1)
        child=self.runs/'20261005-120001-1234abcd';child.mkdir()
        bridge.write_json(child/'state.json',{**self.state,'id':child.name,'parent_task_id':TASK,'session_id':SESSION})
        with patch.object(desktop_client,'call',return_value=self.native()):
            _,data,_=mcp_server.tool_recovery({'task_id':TASK})
        self.assertEqual(data['decision'],'inspect_original');self.assertFalse(data['continuation_candidate'])

    def test_native_cancel_after_monitor_exit_targets_original_task_without_process_kill(self):
        self.interrupted_state(session_id=SESSION)
        with patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',side_effect=[self.native('running'),self.native('cancelled')]) as call,patch.object(bridge.subprocess,'Popen') as spawn:
            text,data,error=mcp_server.tool_cancel({'task_id':TASK})
        self.assertFalse(error);self.assertEqual(data['status'],'cancelled')
        self.assertEqual([c.args[0] for c in call.call_args_list],['status','cancel'])
        self.assertFalse((self.run/'cancel.request').exists());spawn.assert_not_called()

    def test_recovery_does_not_offer_continuation_when_original_status_cannot_be_verified(self):
        bridge.write_json(self.run/'state.json',{**self.state,'status':'failed','session_id':SESSION})
        with patch.object(desktop_client,'call',side_effect=RuntimeError('offline')):
            _,data,_=mcp_server.tool_recovery({'task_id':TASK})
            with self.assertRaisesRegex(mcp_server.ToolError,'尚未确认'):mcp_server._resolve_continuation(TASK,self.root,'read-only')
        self.assertEqual(data['decision'],'check_connection');self.assertFalse(data['continuation_candidate'])

    def test_missing_native_task_does_not_become_a_continuation_candidate(self):
        self.interrupted_state(session_id=SESSION)
        with patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',side_effect=RuntimeError('Task not managed by this connector')):
            _,data,_=mcp_server.tool_recovery({'task_id':TASK})
        self.assertFalse(data['continuation_candidate']);self.assertTrue(data['execution_uncertain'])
        self.assertEqual(data['status'],'running')
        self.assertEqual(data['decision'],'inspect_original');self.assertTrue(data['native_task_missing'])
        with patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',side_effect=RuntimeError('offline')):
            _,offline,_=mcp_server.tool_recovery({'task_id':TASK})
        self.assertEqual(offline['decision'],'check_connection');self.assertFalse(offline['native_task_missing'])
        # Even a completed historical human-owned task must not claim live ownership.
        bridge.write_json(self.run/'state.json',{**self.state,'status':'completed','control':'human','session_id':SESSION})
        with patch.object(desktop_client,'call',side_effect=RuntimeError('Task not managed by this connector')):
            text,data,_=mcp_server.tool_recovery({'task_id':TASK})
        self.assertEqual(data['decision'],'inspect_original');self.assertIn('历史记录',text)

    def test_cancel_race_rejection_does_not_schedule_cancel_after_user_takes_over(self):
        self.interrupted_state(session_id=SESSION)
        with patch.object(bridge,'worker_alive',return_value=False),patch.object(desktop_client,'call',side_effect=[self.native('running'),RuntimeError('用户已接手')]) as call:
            with self.assertRaisesRegex(mcp_server.ToolError,'用户已接手'):
                mcp_server.tool_cancel({'task_id':TASK})
        self.assertEqual([c.args[0] for c in call.call_args_list],['status','cancel'])
        self.assertFalse((self.run/'cancel.request').exists())

if __name__=='__main__':unittest.main()
