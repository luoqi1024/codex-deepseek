"""Behavioral checks for bounded delegation. Temporary files/mocks, no models."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import bridge
import mcp_server
from test_mcp import McpTestBase, _FakeProc, SESSION


class AssignmentTests(McpTestBase):
    def start(self):
        text,data,error,_,_=self.submit_with_mocks({'workspace':str(self.workspace),'task':'修复设置持久化',
            'acceptance_criteria':['重启后设置保留','无效输入有明确提示'],
            'change_scope':['设置页面与相关存储代码'],'optional_improvements':['变量命名调整']})
        self.assertFalse(error,text)
        return data['task_id']

    def stop(self,task,status='completed'):
        path=self.runs/task
        state=bridge.read_json(path/'state.json')
        state.update(status=status,session_id=SESSION)
        bridge.write_json(path/'state.json',state)
        return path

    def review(self,task,outcomes=('failed','passed'),verdict='changes-needed',suggestions=None):
        checks=[{'criterion_id':f'C{i+1}','outcome':outcome,
                 'evidence':'重启后仍恢复默认值' if outcome=='failed' else '实际运行检查结果'} for i,outcome in enumerate(outcomes)]
        return self.tool('deepseek_review',{'task_id':task,'text':'按原条件核对',
            'verdict':verdict,'checks':checks,'suggestions':suggestions or []})

    def next(self,parent,**extra):
        with patch.object(bridge.subprocess,'Popen',return_value=_FakeProc()) as spawn:
            text,data,error=self.tool('deepseek_submit',{'workspace':str(self.workspace),'task':'仅修复持久化问题',
                'parent_task_id':parent,'repair_reason':'修复保存与读取路径不一致，复查重启后保留',**extra})
        return text,data,error,spawn

    def test_first_submission_requires_conditions_without_allocating_or_spawning(self):
        with patch.object(bridge.subprocess,'Popen') as spawn:
            text,_,error=self.tool('deepseek_submit',{'workspace':str(self.workspace),'task':'随便做点'})
        self.assertTrue(error);self.assertIn('acceptance_criteria',text);spawn.assert_not_called()
        self.assertEqual(list(self.runs.iterdir()),[])

    def test_conditions_are_frozen_and_third_execution_is_refused(self):
        first=self.start();path=self.stop(first)
        frozen=(path/'assignment.json').read_bytes()
        text,_,error=self.review(first);self.assertFalse(error,text)
        text,second,error,spawn=self.next(first)
        self.assertFalse(error,text);spawn.assert_called_once()
        child=second['task_id'];child_path=self.stop(child)
        state=bridge.read_json(child_path/'state.json')
        self.assertEqual(state['assignment_id'],first);self.assertEqual(state['attempt_number'],2)
        self.assertEqual((path/'assignment.json').read_bytes(),frozen)
        prompt=bridge.desktop_prompt(child_path,state)
        self.assertIn('重启后设置保留',prompt);self.assertIn('重启后仍恢复默认值',prompt)
        self.assertEqual(second['assignment']['attempts_remaining'],0)
        text,_,error=self.review(child);self.assertFalse(error,text)
        text,_,error,spawn=self.next(child,title='换个标题继续')
        self.assertTrue(error);self.assertIn('两轮',text);spawn.assert_not_called()
        self.assertEqual(len(list(self.runs.glob('*/state.json'))),2)
        text,_,error,spawn=self.next(first)
        self.assertTrue(error);spawn.assert_not_called()
        _,status,error=self.tool('deepseek_status',{'task_id':child})
        self.assertFalse(error);self.assertEqual(status['stage'],'attempts_exhausted')
        self.assertEqual(status['assignment']['attempts_reserved'],2)

    def test_changing_criteria_on_continuation_does_not_consume_second_attempt(self):
        first=self.start();self.stop(first);self.review(first)
        text,_,error,spawn=self.next(first,acceptance_criteria=['顺便重新设计整个应用'])
        self.assertTrue(error);self.assertIn('不能重写约定',text);spawn.assert_not_called()
        self.assertEqual(len(bridge.read_json(self.runs/first/'assignment-budget.json')['attempts']),1)

    def test_optional_improvements_cannot_make_a_passed_task_fail(self):
        first=self.start();self.stop(first)
        text,_,error=self.review(first,('passed','passed'),'changes-needed',['想换一种命名'])
        self.assertTrue(error);self.assertIn('可选建议',text)
        self.assertFalse((self.runs/first/'review.json').exists())
        text,data,error=self.review(first,('passed','passed'),'accepted',['想换一种命名'])
        self.assertFalse(error,text);self.assertEqual(data['suggestions'],['想换一种命名'])
        text,_,error,spawn=self.next(first)
        self.assertTrue(error);self.assertIn('已验收通过',text);spawn.assert_not_called()

    def test_worker_final_write_cannot_hide_a_persisted_review(self):
        first=self.start();path=self.stop(first)
        self.review(first,('passed','passed'),'accepted')
        state=bridge.read_json(path/'state.json');state.pop('review',None)
        bridge.write_json(path/'state.json',state)
        self.assertEqual(bridge.current_state(path)['review'],'accepted')
        text,_,error,spawn=self.next(first)
        self.assertTrue(error);self.assertIn('已验收通过',text);spawn.assert_not_called()

    def test_unverified_is_neither_failure_nor_pass(self):
        first=self.start();self.stop(first)
        for verdict in ('accepted','changes-needed'):
            text,_,error=self.review(first,('unverified','passed'),verdict)
            self.assertTrue(error);self.assertIn('未核实',text)
        text,data,error=self.review(first,('unverified','passed'),'inconclusive')
        self.assertFalse(error,text);self.assertEqual(data['review'],'inconclusive')
        _,status,error=self.tool('deepseek_status',{'task_id':first})
        self.assertFalse(error);self.assertEqual(status['stage'],'review_inconclusive')

    def test_review_requires_original_ids_complete_coverage_and_evidence(self):
        first=self.start();self.stop(first)
        good=[{'criterion_id':'C1','outcome':'failed','evidence':'重启后复现'},
              {'criterion_id':'C2','outcome':'passed','evidence':'实际输入检查'}]
        cases=[good[:1],[{**good[0],'criterion_id':'C3'},good[1]],
               [good[0],{**good[1],'criterion_id':'C1'}],[{**good[0],'evidence':' '},good[1]]]
        for checks in cases:
            with self.subTest(checks=checks):
                _,_,error=self.tool('deepseek_review',{'task_id':first,'text':'返工','verdict':'changes-needed','checks':checks})
                self.assertTrue(error)
        self.assertFalse((self.runs/first/'review.json').exists())

    def test_second_execution_requires_recorded_review_and_specific_reason(self):
        first=self.start();self.stop(first)
        text,_,error,spawn=self.next(first)
        self.assertTrue(error);spawn.assert_not_called()
        self.review(first)
        text,_,error,spawn=self.next(first,repair_reason=' ')
        self.assertTrue(error);self.assertIn('repair_reason',text);spawn.assert_not_called()

    def test_execution_failure_and_repair_share_one_budget(self):
        first=self.start();self.stop(first,'failed')
        self.review(first,('unverified','unverified'),'inconclusive')
        text,data,error,_=self.next(first,repair_reason='已修复指定运行时路径，按原任务继续')
        self.assertFalse(error,text);second=data['task_id'];self.stop(second,'failed')
        self.review(second,('unverified','unverified'),'inconclusive')
        text,_,error,spawn=self.next(second)
        self.assertTrue(error);self.assertIn('两轮',text);spawn.assert_not_called()

    def test_contract_damage_does_not_reset_budget_or_spawn_worker(self):
        first=self.start();path=self.stop(first);self.review(first)
        contract=bridge.read_json(path/'assignment.json');contract['acceptance_criteria'].append('新增目标')
        bridge.write_json(path/'assignment.json',contract)
        text,_,error,spawn=self.next(first)
        self.assertTrue(error);self.assertIn('不一致',text);spawn.assert_not_called()
        self.assertEqual(len(bridge.read_json(path/'assignment-budget.json')['attempts']),1)

    def test_spawn_failure_keeps_reservation_and_never_refunds_automatically(self):
        with patch.object(bridge.subprocess,'Popen',side_effect=RuntimeError('启动响应丢失')):
            _,_,error=self.tool('deepseek_submit',{'workspace':str(self.workspace),'task':'只读检查',
                'acceptance_criteria':['给出实际结论'],'change_scope':['指定文件']})
        self.assertTrue(error)
        roots=list(self.runs.glob('*/assignment-budget.json'))
        self.assertEqual(len(roots),1);self.assertEqual(len(bridge.read_json(roots[0])['attempts']),1)

    def test_cli_submit_uses_same_policy_and_old_worker_cannot_dispatch(self):
        task_file=self.root/'task.txt';task_file.write_text('任务',encoding='utf8')
        args=SimpleNamespace(workspace=str(self.workspace),task_file=str(task_file),title='标题',
            permission='read-only',timeout=60,no_dashboard=True)
        with bridge.submission_lock(),patch.object(bridge.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'首次提交'):bridge.submit(args)
        spawn.assert_not_called()
        path=self.run_dir(state={'status':'queued'})
        with patch('sys.argv',['bridge.py','worker',path.name]),patch.object(bridge,'worker') as worker:
            with self.assertRaisesRegex(ValueError,'旧任务'):bridge.main()
        worker.assert_not_called();self.assertEqual(bridge.read_json(path/'state.json')['status'],'failed')

    def test_worker_reservation_can_start_once_and_keeps_original_result(self):
        first=self.start();path=self.runs/first
        with patch('sys.argv',['bridge.py','worker',first]),patch.object(bridge,'worker') as worker:
            bridge.main()
            worker.assert_called_once_with(first)
        self.stop(first)
        (path/'result.md').write_text('已有结果',encoding='utf8')
        before=(path/'state.json').read_bytes()
        with patch('sys.argv',['bridge.py','worker',first]),patch.object(bridge,'worker') as worker:
            with self.assertRaisesRegex(ValueError,'重复启动'):bridge.main()
        worker.assert_not_called();self.assertEqual((path/'state.json').read_bytes(),before)
        self.assertEqual((path/'result.md').read_text(encoding='utf8'),'已有结果')

    def test_old_worker_and_missing_budget_fail_closed(self):
        first=self.start();self.stop(first);self.review(first)
        _,data,error,_=self.next(first);self.assertFalse(error)
        with self.assertRaisesRegex(ValueError,'旧 worker'):
            bridge.policy.verify_worker(self.runs,bridge.read_json(self.runs/first/'state.json'))
        budget=self.runs/first/'assignment-budget.json'
        budget.write_text('broken',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'缺失/损坏'):
            bridge.policy.verify_worker(self.runs,bridge.read_json(self.runs/data['task_id']/'state.json'))

    def test_cli_third_attempt_is_blocked_without_resetting_the_chain(self):
        first=self.start();self.stop(first);self.review(first)
        _,data,error,_=self.next(first);self.assertFalse(error)
        second=data['task_id'];self.stop(second);self.review(second)
        task_file=self.root/'cli-task.txt';task_file.write_text('再试一次',encoding='utf8')
        args=SimpleNamespace(workspace=str(self.workspace),task_file=str(task_file),title='新标题',
            permission='read-only',timeout=60,no_dashboard=True,parent_task_id=second,
            session_id=SESSION,repair_reason='继续修复')
        with bridge.submission_lock(),patch.object(bridge.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'两轮'):bridge.submit(args)
        spawn.assert_not_called();self.assertEqual(len(list(self.runs.glob('*/state.json'))),2)

    def test_concurrent_worker_processes_claim_only_one_reservation(self):
        first=self.start()
        helper=self.root/'claim.py'
        helper.write_text("""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[3])
import bridge
bridge.ROOT=Path(sys.argv[1]);bridge.RUNS=bridge.ROOT/'runs'
task_id=sys.argv[2];sys.argv=['bridge.py','worker',task_id]
def fake_worker(task_id):
    with (bridge.ROOT/'claims.txt').open('a',encoding='utf8') as handle:handle.write(task_id+'\\n')
bridge.worker=fake_worker
bridge.main()
""",encoding='utf8')
        command=[sys.executable,'-X','utf8',str(helper),str(self.root),first,str(bridge.policy.__file__.rsplit(os.sep,1)[0])]
        processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0) for _ in range(2)]
        for proc in processes:proc.communicate(timeout=20)
        self.assertEqual(sorted(proc.returncode for proc in processes),[0,1])
        self.assertEqual((self.root/'claims.txt').read_text(encoding='utf8').splitlines(),[first])


if __name__=='__main__':unittest.main()
