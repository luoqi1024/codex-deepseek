import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { randomUUID } from 'node:crypto';
import { Connector } from './native_connector.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-native-test-'));
  const workspace = path.join(root, '项目'); fs.mkdirSync(workspace);
  const runs = path.join(root, 'runs'); fs.mkdirSync(runs);
  const listeners = new Map(), agents = new Map(), workspaces = new Map();
  const emitted = []; let prompts = 0, cancellations = 0;
  const emit = (name, ...args) => { for (const f of listeners.get(name) ?? []) f(...args); };
  const ctx = {
    on(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); },
    agents: { get(id) { return agents.get(id); }, list() { return [...agents.values()]; } },
    agentDefaultModel: { currentSelection() { return { provider: 'opencode-go', model: 'deepseek-v4.1-flash' }; } },
    workspaceRegistry: { async create(cwd) {
      if (!workspaces.has(cwd)) workspaces.set(cwd, { id: 'workspace-' + randomUUID(), path: cwd });
      return workspaces.get(cwd);
    } },
    commands: { register(command) { ctx.command = command; } },
    sessionController: {
      async create(req) {
        const id = req.sessionId ?? 'session-' + randomUUID();
        if (!agents.has(id)) {
          const session = { id, header: { cwd: workspaces.get(fs.realpathSync(workspace)).path } };
          agents.set(id, { id, session, inbox: {hasPending:false}, status: 'idle', cancel() { cancellations++; this.status = 'idle'; }, async whenIdle() {} });
        }
        return { sessionId: id };
      },
      async resolveAgent(id) { return { agent: agents.get(id) }; },
      async rename(req) { agents.get(req.sessionId).title = req.title; },
      async selectModel(req) { assert.equal(req.provider, 'opencode-go'); },
      async prompt(req, signal) {
        signal.throwIfAborted();
        prompts++; const agent = agents.get(req.sessionId); agent.status = 'running';
        emit('agent/inbox/inserted', { agent, message: { source: { kind: 'user', rpcId: req.requestId } } });
      }
    }
  };
  const connector = new Connector(ctx, { runsDirectory: runs, stateDirectory: path.join(root, 'state') }, {
    setSandboxMode(s, mode) { emitted.push(['sandbox', mode]); }, setApprovalPolicy(s, mode) { emitted.push(['approval', mode]); }
  }); connector.wire();
  t.after(async () => {
    for (const timer of connector.timers.values()) clearTimeout(timer);
    if (connector.server) await connector.close();
    assert.ok(path.resolve(root).startsWith(path.join(os.tmpdir(), 'codex-native-test-')));
    fs.rmSync(root, { recursive: true, force: true });
  });
  function request(n = 1, extra = {}) {
    const id = '20261005-120000-' + n.toString(16).padStart(8, '0');
    const req = { task_id: id, workspace, title: '中文演示', task: 'Read only. $(literal)', permission: 'read-only', timeout_seconds: 60, ...extra };
    const run = path.join(runs, id); fs.mkdirSync(run, { recursive: true });
    fs.writeFileSync(path.join(run, 'state.json'), JSON.stringify({ id, workspace, permission: req.permission }));
    return req;
  }
  return { connector, ctx, agents, workspaces, emit, emitted, request, runs, workspace,
    get prompts() { return prompts; }, get cancellations() { return cancellations; } };
}

test('public usage counters include retries and compaction, redact to numeric fields, stop at human takeover', async t => {
  const f=fixture(t), req=f.request(), result=await f.connector.submit(req);
  const agent=f.agents.get(result.session_id);
  f.emit('session/event', agent.session, {type:'step/start', data:{turn:1,step:1}});
  const usage={inputTokens:100,outputTokens:20,cacheReadTokens:900,reasoningTokens:5,secret:'NEVER_LOG'};
  f.emit('session/event', agent.session, {type:'assistant/attempt',seq:11,
    data:{turn:1,step:1,stream:[{type:'chunk',time:Date.now(),chunk:{type:'usage',usage}}]}});
  f.emit('session/event', agent.session, {type:'assistant/message',seq:12,
    data:{turn:1,step:1,usage,message:{source:{provider:'opencode-go',model:'deepseek-v4.1-flash'},content:[]},
      stream:[{type:'chunk',chunk:{type:'usage',usage}}]}});
  f.emit('session/event', agent.session, {type:'assistant/attempt',seq:13,data:{stream:[]}});
  f.emit('session/event', agent.session, {type:'compaction/summary',seq:14,
    data:{llmStreamCall:true,usage,provider:'opencode-go',model:'deepseek-v4.1-flash'}});
  f.connector.takeover(agent.id);
  f.emit('session/event', agent.session, {type:'assistant/message',seq:15,data:{usage,message:{content:[]}}});
  const log=fs.readFileSync(path.join(f.runs,req.task_id,'events.jsonl'),'utf8');
  assert.ok(!log.includes('NEVER_LOG'));
  const records=log.trim().split('\n').map(JSON.parse).filter(x=>x.type==='usage');
  assert.equal(records.length,4); assert.equal(records[0].usage.cacheReadTokens,900);
  assert.equal(records[1].seq,12); assert.equal(records[1].provider,'opencode-go');
  assert.ok(records[1].started); assert.equal(records[2].usage,null);
  assert.equal(records[3].source,'compaction/summary');
});

test('real session policy is confined; repeated submission never sends twice', async t => {
  const f = fixture(t), req = f.request(); const result = await f.connector.submit(req);
  assert.equal(result.control, 'codex'); assert.equal(result.desktop_visible, true);
  assert.deepEqual(f.emitted, [['sandbox', 'read-only'], ['approval', 'never']]);
  await f.connector.submit(req); assert.equal(f.prompts, 1);
});
test('completed native content is logged, reasoning omitted, and workspace reused on continuation', async t => {
  const f = fixture(t), req = f.request(); const result = await f.connector.submit(req);
  const agent = f.agents.get(result.session_id);
  f.emit('session/event', agent.session, { type: 'assistant/message', data: { message: { content: [
    { type: 'thinking', text: 'PRIVATE_REASONING' }, { type: 'text', text: '完成 中文' },
    { type: 'tool-call', id: 'c1', name: 'read', arguments: '{}' }] } } });
  f.emit('session/event', agent.session, { type: 'turn/end', data: { reason: { kind: 'completed' } } }); agent.status = 'idle';
  assert.equal(f.connector.get(req.task_id).status, 'completed');
  const log = fs.readFileSync(path.join(f.runs, req.task_id, 'events.jsonl'), 'utf8');
  assert.ok(!log.includes('PRIVATE_REASONING')); assert.ok(log.includes('tool_call'));
  assert.equal(fs.readFileSync(path.join(f.runs, req.task_id, 'result.md'), 'utf8'), '完成 中文');
  const next = await f.connector.submit(f.request(2, { parent_task_id: req.task_id, session_id: result.session_id }));
  assert.equal(next.session_id, result.session_id); assert.equal(f.workspaces.size, 1);
});
test('native client input takes ownership without discarding user work; no subsequent cancel or continuation', async t => {
  const f = fixture(t), req = f.request(), result = await f.connector.submit(req);
  f.emit('agent/inbox/inserted', { agent: f.agents.get(result.session_id), message: { source: { kind: 'user', rpcId: 'client-input' } } });
  assert.equal(f.connector.get(req.task_id).status, 'handed_off');
  assert.equal(f.cancellations, 0); assert.equal(f.connector.timers.size, 0);
  await assert.rejects(f.connector.stop(req.task_id), /用户已接手/);
  await assert.rejects(f.connector.submit(f.request(2, { parent_task_id: req.task_id, session_id: result.session_id })), /客户端任务|接手/);
});
test('explicit handoff stops an active delegate first and is idempotent', async t => {
  const f = fixture(t), req = f.request(); await f.connector.submit(req);
  const result = await f.connector.handoff(req.task_id);
  assert.equal(result.control, 'human'); assert.equal(f.cancellations, 1);
  await f.connector.handoff(req.task_id); assert.equal(f.cancellations, 1);
});
test('unknown sessions cannot be adopted and permission cannot increase', async t => {
  const f = fixture(t);
  await assert.rejects(f.connector.submit(f.request(1, { session_id: 'session-' + randomUUID() })), /managed parent/);
  const req = f.request(2), result = await f.connector.submit(req);
  await f.connector.stop(req.task_id);
  await assert.rejects(f.connector.submit(f.request(3, { parent_task_id: req.task_id, session_id: result.session_id, permission: 'workspace-write' })), /permission mismatch/);
});
test('active overlapping workspace rejects second delegate', async t => {
  const f = fixture(t); await f.connector.submit(f.request());
  await assert.rejects(f.connector.submit(f.request(2)), /overlaps/);
});
test('errors and timeout retain native host and mark actual outcome', async t => {
  const f = fixture(t), req = f.request(), result = await f.connector.submit(req);
  f.emit('agent/error', { agent: f.agents.get(result.session_id), error: new Error('provider failed') });
  assert.equal(f.connector.get(req.task_id).status, 'failed');
  f.agents.get(result.session_id).status = 'idle';
  const req2 = f.request(2); await f.connector.submit(req2);
  await f.connector.stop(req2.task_id, 'timed_out');
  assert.equal(f.connector.get(req2.task_id).status, 'timed_out'); assert.equal(f.cancellations, 1);
});
test('another client agent on the workspace is awaited before admitting its step', async t => {
  const f = fixture(t), req = f.request(); await f.connector.submit(req);
  const agent = { id: 'other-native-session', session: { header: { cwd: f.workspace } } };
  const hook = [...f.ctx.agents.list()]; assert.equal(hook.length, 1);
  // Execute the registered waterfall listener through a fixture emitter with an awaitable next.
  let entered = false;
  f.emit('agent/pre-step', { agent }, () => { entered = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(entered); assert.equal(f.cancellations, 1);
  assert.equal(f.connector.get(req.task_id).owner, 'human');
});
test('capability endpoint rejects browser origins, unauthenticated requests, and arbitrary operations', async t => {
  const f = fixture(t); await f.connector.listen();
  const url = 'http://127.0.0.1:' + f.connector.server.address().port + '/rpc';
  const options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"operation":"health"}' };
  assert.equal((await fetch(url, options)).status, 403);
  const authenticated = { ...options, headers: { ...options.headers, Authorization: 'Bearer ' + f.connector.token } };
  assert.equal((await fetch(url, authenticated)).status, 200);
  assert.equal((await fetch(url, { ...authenticated, headers: { ...authenticated.headers, Origin: 'https://malicious.invalid' } })).status, 403);
  assert.equal((await fetch(url, { ...authenticated, body: '{"operation":"eval","code":"bad"}' })).status, 400);
});
test('host restart never reruns work and preserves human ownership', async t => {
  const f = fixture(t), req = f.request(), result = await f.connector.submit(req);
  const restored = new Connector(f.ctx, f.connector.config, f.connector.policy);
  assert.equal(restored.get(req.task_id).status, 'failed'); assert.equal(f.prompts, 1);
  await f.connector.handoff(req.task_id);
  const restored2 = new Connector(f.ctx, f.connector.config, f.connector.policy);
  assert.equal(restored2.publicTask(restored2.get(req.task_id)).control, 'human');
});

test('explicit return retains native history and permissions without sending or cancelling work', async t => {
  const f = fixture(t), req = f.request(), result = await f.connector.submit(req);
  await f.connector.handoff(req.task_id);
  const returned = await f.connector.returnControl(req.task_id);
  assert.equal(returned.control, 'codex'); assert.equal(returned.session_id, result.session_id);
  assert.equal(returned.permission, 'read-only'); assert.equal(f.prompts, 1); assert.equal(f.cancellations, 1);
  await f.connector.returnControl(req.task_id);
  const next = await f.connector.submit(f.request(2, {parent_task_id:req.task_id,session_id:result.session_id}));
  assert.equal(next.session_id, result.session_id); assert.equal(f.prompts, 2);
  await assert.rejects(f.connector.returnControl(req.task_id), /最新/);
});

test('return rejects running, queued, overlapping and unknown client state without cancelling it', async t => {
  const f = fixture(t), req = f.request(), result = await f.connector.submit(req);
  f.emit('agent/inbox/inserted', {agent:f.agents.get(result.session_id), message:{source:{kind:'user',rpcId:'human'}}});
  await assert.rejects(f.connector.returnControl(req.task_id), /仍在运行/);
  const agent = f.agents.get(result.session_id); agent.status = 'idle'; agent.inbox.hasPending = true;
  await assert.rejects(f.connector.returnControl(req.task_id), /排队/);
  agent.inbox.hasPending = false;
  const other = {id:'other',session:{header:{cwd:f.workspace}},status:'idle',inbox:{hasPending:true}};
  f.agents.set('other', other);
  await assert.rejects(f.connector.returnControl(req.task_id), /排队/);
  f.agents.delete('other'); delete agent.inbox;
  await assert.rejects(f.connector.returnControl(req.task_id), /排队/);
  assert.equal(f.cancellations, 0); assert.equal(f.connector.get(req.task_id).owner, 'human');
});

test('human input after return immediately restores human ownership and restart preserves return', async t => {
  const f = fixture(t), req = f.request(), result = await f.connector.submit(req);
  await f.connector.handoff(req.task_id); await f.connector.returnControl(req.task_id);
  const restored = new Connector(f.ctx, f.connector.config, f.connector.policy);
  assert.equal(restored.get(req.task_id).owner, 'codex');
  f.emit('agent/inbox/inserted', {agent:f.agents.get(result.session_id),message:{source:{kind:'user',rpcId:'human-new'}}});
  assert.equal(f.connector.get(req.task_id).owner, 'human');
  await assert.rejects(f.connector.stop(req.task_id), /用户已接手/);
});
