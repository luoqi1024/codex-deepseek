"""Frozen acceptance criteria and durable two-attempt reservations. No model calls.

Call prepare/reserve/review under bridge.submission_lock. Records are an ordinary
local guardrail, not protection against a user modifying files or creating a new goal.
"""
import hashlib
import json
from pathlib import Path
import re

LIMIT = 2
ID = re.compile(r"\d{8}-\d{6}-[a-f0-9]{8}")
VERDICTS = ('accepted', 'changes-needed', 'inconclusive')


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('任务约定或次数记录缺失/损坏；先核对原任务，不自动重建计数。') from exc


def _strings(value, label, required=True, maximum=10):
    if value is None and not required:
        return []
    if not isinstance(value, list) or not (int(required) <= len(value) <= maximum):
        raise ValueError(f'{label} 必须是含 {int(required)}..{maximum} 条内容的数组')
    if any(not isinstance(x, str) or not x.strip() or len(x) > 600 for x in value):
        raise ValueError(f'{label} 每条必须为非空字符串，最多 600 字符')
    return [x.strip() for x in value]


def contract_input(value):
    if not isinstance(value, dict) or set(value) - {'acceptance_criteria', 'change_scope', 'optional_improvements'}:
        raise ValueError('首次提交必须提供 acceptance_criteria、change_scope；约定不支持额外参数')
    return {'acceptance_criteria': _strings(value.get('acceptance_criteria'), 'acceptance_criteria'),
            'change_scope': _strings(value.get('change_scope'), 'change_scope'),
            'optional_improvements': _strings(value.get('optional_improvements'), 'optional_improvements', False, 5)}


def digest(contract):
    return hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def load(runs, state):
    assignment = state.get('assignment_id')
    if not isinstance(assignment, str) or not ID.fullmatch(assignment):
        raise ValueError('旧任务缺少固定验收约定，不能自动续接或重置次数；保留历史，先核对已有成果。')
    root = runs / assignment
    contract, budget = _read(root / 'assignment.json'), _read(root / 'assignment-budget.json')
    hashed = digest(contract)
    attempts = budget.get('attempts') if isinstance(budget, dict) else None
    if (not isinstance(contract, dict) or not isinstance(budget, dict) or contract.get('version') != 1 or contract.get('assignment_id') != assignment
            or hashed != state.get('contract_hash') or budget.get('contract_hash') != hashed
            or budget.get('attempt_limit') != LIMIT or not isinstance(attempts, list) or not 1 <= len(attempts) <= LIMIT):
        raise ValueError('任务约定/次数记录不一致，拒绝重置或继续派发。')
    ids = [x.get('task_id') if isinstance(x, dict) else None for x in attempts]
    if (any(not isinstance(x, str) or not ID.fullmatch(x) for x in ids) or len(set(ids)) != len(ids)
            or ids[0] != assignment or state.get('id') not in ids
            or type(state.get('attempt_number')) is not int or state['attempt_number'] != ids.index(state['id']) + 1):
        raise ValueError('任务链次数记录不一致，拒绝派发。')
    if (not isinstance(state.get('workspace'), str) or not isinstance(contract.get('workspace'), str)
            or Path(state['workspace']).resolve() != Path(contract['workspace']).resolve()
            or state.get('permission') not in ('read-only', 'workspace-write')
            or contract.get('permission') not in ('read-only', 'workspace-write')
            or contract['permission'] == 'read-only' and state['permission'] != 'read-only'):
        raise ValueError('执行记录超出原任务工作区或权限，拒绝派发。')
    return root, contract, budget


def prepare(runs, parent, supplied, reason):
    if parent is None:
        if reason is not None:
            raise ValueError('repair_reason 仅用于原任务的第二轮修正，不得另建任务绕过上限。')
        return {'input': contract_input(supplied)}
    root, contract, budget = load(runs, parent)
    if supplied is not None:
        raise ValueError('续接必须沿用固定验收条件和范围，不能重写约定。')
    if len(budget['attempts']) >= LIMIT:
        raise ValueError('同一任务已预留两轮执行，返工/故障重试共用上限；停止自动派发，报告剩余问题。')
    if budget['attempts'][-1]['task_id'] != parent['id']:
        raise ValueError('任务链已有后续提交，只能检查最新任务，不能从旧父任务分叉返工。')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1200:
        raise ValueError('第二轮必须提供 repair_reason，说明具体问题与修正办法（最多 1200 字符）。')
    review = _read(runs / parent['id'] / 'review.json')
    if review.get('contract_hash') != digest(contract) or review.get('task_id') != parent['id']:
        raise ValueError('验收批注与原任务约定不一致。')
    normalized = validate_review(contract, review.get('verdict'), review.get('checks'), review.get('suggestions'))
    if normalized['verdict'] == 'accepted':
        raise ValueError('原任务已验收通过，可选改善不能触发返工；新目标需用户明确授权。')
    return {'root': root, 'contract': contract, 'budget': budget, 'reason': reason.strip(), 'review': normalized}


def reserve(plan, path, state, write_json):
    if 'input' in plan:
        contract = {'version': 1, 'assignment_id': state['id'], 'goal': state['title'],
                    'workspace': state['workspace'], 'permission': state['permission'], **plan['input']}
        root = path
        hashed = digest(contract)
        budget = {'contract_hash': hashed, 'attempt_limit': LIMIT, 'attempts': []}
        write_json(path / 'assignment.json', contract)
    else:
        root, contract, budget = plan['root'], plan['contract'], plan['budget']
        hashed = digest(contract)
    number = len(budget['attempts']) + 1
    budget['attempts'].append({'task_id': state['id'], 'reserved_at': state['created'],
                               'reason': plan.get('reason', '首次执行')})
    # Reserve before starting a worker. Ambiguous admissions never refund themselves.
    write_json(root / 'assignment-budget.json', budget)
    state.update(assignment_id=contract['assignment_id'], contract_hash=hashed, attempt_number=number)
    if plan.get('reason'):
        state['repair_reason'] = plan['reason']
        state['repair_checks'] = [x for x in plan['review']['checks'] if x['outcome'] != 'passed']


def validate_review(contract, verdict, checks, suggestions=None):
    if verdict not in VERDICTS:
        raise ValueError('verdict 必须为 accepted、changes-needed 或 inconclusive')
    if not isinstance(checks, list) or len(checks) != len(contract['acceptance_criteria']):
        raise ValueError('checks 必须逐条覆盖原验收条件，不得新增或遗漏条件。')
    required = {f'C{i+1}' for i in range(len(contract['acceptance_criteria']))}
    seen, result = set(), []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'criterion_id', 'outcome', 'evidence'}:
            raise ValueError('每条检查需要 criterion_id、outcome、evidence')
        criterion, outcome, evidence = check['criterion_id'], check['outcome'], check['evidence']
        if not isinstance(criterion, str) or criterion not in required or criterion in seen:
            raise ValueError('criterion_id 必须对应原条件 C1、C2…，不得重复或新增。')
        if outcome not in ('passed', 'failed', 'unverified'):
            raise ValueError('outcome 必须为 passed、failed 或 unverified')
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1200:
            raise ValueError('每条验收必须提供具体 evidence（最多 1200 字符），不能只写偏好。')
        seen.add(criterion)
        result.append({**check, 'evidence': evidence.strip()})
    outcomes = {x['outcome'] for x in result}
    expected = 'changes-needed' if 'failed' in outcomes else 'inconclusive' if 'unverified' in outcomes else 'accepted'
    if verdict != expected:
        raise ValueError('验收结论与检查不一致：未核实不能算通过或失败，可选建议不阻止通过。')
    return {'verdict': verdict, 'checks': result,
            'suggestions': _strings(suggestions, 'suggestions', False, 5)}


def summary(runs, state):
    if not state.get('assignment_id'):
        return {'policy': 'legacy', 'continuation_budget_available': False}
    try:
        _, contract, budget = load(runs, state)
    except ValueError as exc:
        return {'policy': 'unavailable', 'continuation_budget_available': False, 'message': str(exc)}
    remaining = LIMIT - len(budget['attempts'])
    return {'policy': 'bounded', 'assignment_id': contract['assignment_id'],
            'attempt_number': state['attempt_number'], 'attempt_limit': LIMIT,
            'attempts_reserved': len(budget['attempts']), 'attempts_remaining': remaining,
            'latest_task_id': budget['attempts'][-1]['task_id'],
            'continuation_budget_available': remaining > 0 and budget['attempts'][-1]['task_id'] == state['id']}


def prompt(runs, state):
    _, contract, _ = load(runs, state)
    criteria = '\n'.join(f'C{i+1}: {text}' for i, text in enumerate(contract['acceptance_criteria']))
    return ('\n\n--- 固定任务约定 ---\n'
            + f"原目标：{contract['goal']}\n允许改动/调查范围：" + '；'.join(contract['change_scope'])
            + '\n必须满足的验收条件：\n' + criteria
            + '\n可选建议不作为通过条件：' + '；'.join(contract['optional_improvements'])
            + f"\n本次为第 {state['attempt_number']}/{LIMIT} 轮；不要扩大目标或自行派发。"
            + ('\n本轮修正理由：' + state['repair_reason'] if state.get('repair_reason') else '')
            + ''.join(f"\nCodex 核对：{x['criterion_id']} {x['outcome']}；证据：{x['evidence']}" for x in state.get('repair_checks', []))
            + '\n最终报告按 C1、C2…说明实际检查和遗留问题，不把未核实写成通过。')


def verify_worker(runs, state):
    _, _, budget = load(runs, state)
    if budget['attempts'][-1]['task_id'] != state['id']:
        raise ValueError('已有更新的执行预留，旧 worker 不得重新启动。')
    if state.get('status') != 'queued':
        raise ValueError('该预留已启动或已结束，不能重复启动 worker；先检查恢复说明。')
