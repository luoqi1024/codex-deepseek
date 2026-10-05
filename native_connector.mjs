/** Cordis host plugin. Uses public Harness services; never reads account tokens. */
import http from 'node:http';
import { randomBytes, timingSafeEqual, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ID = /^\d{8}-\d{6}-[a-f0-9]{8}$/;
const SESSION = /^session-[a-f0-9-]{36}$/;
const ACTIVE = new Set(['queued', 'running']);
const PROTOCOL = 'codex-deepseek-desktop-v1';
const MAX_BODY = 1024 * 1024;
const stamp = () => new Date().toISOString();
const sameTree = (a, b) => {
  a = path.resolve(a).toLowerCase(); b = path.resolve(b).toLowerCase();
  return a === b || a.startsWith(b + path.sep) || b.startsWith(a + path.sep);
};
function atomic(file, data) {
  const tmp = file + '.' + randomUUID() + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, file);
}
function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) =>
    [k, /^(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)$/i.test(k) ? '[REDACTED]' : redact(v)]));
  if (typeof value !== 'string') return value;
  return value.replace(/\bBearer\s+[^\s"']+/gi, 'Bearer [REDACTED]').replace(/\bsk-[A-Za-z0-9_-]{12,}/g, '[REDACTED]')
    .replace(/((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)(["']?)[^\s,"']+/gi, '$1[REDACTED]');
}
function textOf(content) {
  return Array.isArray(content) ? content.filter(x => x?.type === 'text').map(x => x.text ?? '').join('\n') : '';
}

export class Connector {
  constructor(ctx, config, policy) {
    this.ctx = ctx; this.config = config; this.policy = policy;
    this.tasks = new Map(); this.bySession = new Map(); this.timers = new Map(); this.gate = Promise.resolve();
    fs.mkdirSync(config.stateDirectory, { recursive: true, mode: 0o700 });
    this.catalog = path.join(config.stateDirectory, 'tasks.json');
    let records = [];
    try { records = JSON.parse(fs.readFileSync(this.catalog, 'utf8')); }
    catch (e) { if (e.code !== 'ENOENT') throw new Error('连接器任务目录损坏，请恢复备份。'); }
    for (const t of records) {
      if (!ID.test(t.id) || !SESSION.test(t.session_id)) throw new Error('连接器任务标识损坏。');
      if (ACTIVE.has(t.status)) Object.assign(t, { status: 'failed', error: 'Harness host restarted; no task was automatically rerun.', finished: stamp() });
      this.tasks.set(t.id, t); this.bySession.set(t.session_id, t.id);
    }
    this.save();
  }
  save() { atomic(this.catalog, [...this.tasks.values()]); }
  get(id) {
    if (!ID.test(id ?? '')) throw new Error('Invalid task id');
    const task = this.tasks.get(id);
    if (!task) throw new Error('Task not managed by this connector');
    return task;
  }
  publicTask(t) {
    const latest = this.tasks.get(this.bySession.get(t.session_id));
    const owner = latest?.owner === 'human' ? 'human' : t.owner;
    return { ...t, owner, backend: 'desktop', desktop_visible: true, control: owner,
      computer_use_available: this.computerUseState().tools_ready === true,
      permission_settings: { sandbox: t.permission, approval: 'never' } };
  }
  log(t, value) {
    const file = path.join(this.config.runsDirectory, t.id, 'events.jsonl');
    fs.appendFileSync(file, JSON.stringify({ time: stamp(), order: Date.now() * 1e6, ...redact(value) }) + '\n', 'utf8');
  }
  serial(fn) {
    const pending = this.gate.then(fn); this.gate = pending.catch(() => {}); return pending;
  }
  computerUseState() {
    return this.policy.computerUseStatus?.() ?? { provider: null, catalog_size: 0, tools_ready: false,
      desktop_actions: 0, model_requests: 0 };
  }
  async submit(req) {
    return this.serial(async () => {
      const computerUse = this.computerUseState().tools_ready === true;
      if (!ID.test(req.task_id ?? '') || !['read-only', 'workspace-write'].includes(req.permission) ||
          typeof req.task !== 'string' || !req.task.trim() || req.task.length > 160000 ||
          typeof req.title !== 'string' || req.title.length > 200 ||
          !Number.isInteger(req.timeout_seconds) || req.timeout_seconds < 10 || req.timeout_seconds > 7200 ||
          !path.isAbsolute(req.workspace ?? '')) throw new Error('Invalid submission');
      if (this.tasks.has(req.task_id)) return this.publicTask(this.get(req.task_id)); // Retry only acknowledges; never sends twice.
      const model = this.ctx.agentDefaultModel.currentSelection();
      if (model?.provider !== 'opencode-go' || model?.model !== 'deepseek-v4.1-flash')
        throw new Error('桌面默认模型不是配置的 OpenCode Go DeepSeek V4.1 Flash，请在 Harness 选择它后重试。');
      const runDir = path.join(this.config.runsDirectory, req.task_id);
      const state = JSON.parse(fs.readFileSync(path.join(runDir, 'state.json'), 'utf8'));
      const cwd = fs.realpathSync(req.workspace);
      if (!fs.statSync(cwd).isDirectory() || state.id !== req.task_id ||
          fs.realpathSync(state.workspace) !== cwd || state.permission !== req.permission)
        throw new Error('Submission differs from the local task record');
      for (const t of this.tasks.values()) if (t.owner === 'codex' && ACTIVE.has(t.status) && sameTree(cwd, t.workspace))
        throw new Error('Workspace overlaps an active desktop task');
      for (const agent of this.ctx.agents.list()) if (agent.status === 'running' && sameTree(cwd, agent.session.header.cwd))
        throw new Error('工作区内已有客户端任务执行中，请等它结束后再委派。');
      let parent;
      if (req.parent_task_id) {
        parent = this.get(req.parent_task_id);
        if (this.bySession.get(parent.session_id) !== parent.id || parent.owner !== 'codex' || ACTIVE.has(parent.status))
          throw new Error('会话已由用户接手、已有新的后续任务或仍在执行，不能由 Codex 续接。');
        if (parent.workspace !== cwd || req.session_id !== parent.session_id ||
            (parent.permission === 'read-only' && req.permission !== 'read-only')) throw new Error('Continuation identity or permission mismatch');
        if (this.ctx.agents.get(parent.session_id)?.status === 'running') throw new Error('Session is busy in the client');
      } else if (req.session_id) throw new Error('Session adoption requires a managed parent task');
      const workspace = await this.ctx.workspaceRegistry.create(cwd);
      const created = await this.ctx.sessionController.create({ workspaceId: workspace.id,
        ...(parent ? { sessionId: parent.session_id } : {}) });
      const found = await this.ctx.sessionController.resolveAgent(created.sessionId);
      if (!found.agent) throw found.error ?? new Error('Could not resolve the native agent');
      const agent = found.agent;
      // Explicit session policy; the desktop user's default can remain full access.
      if (this.policy.setPreset) this.policy.setPreset(agent.session, req.permission);
      else {
        this.policy.setSandboxMode(agent.session, req.permission);
        this.policy.setApprovalPolicy(agent.session, 'never');
      }
      // Pin the configured model on this session; the already matching default is retained.
      await this.ctx.sessionController.selectModel({ sessionId: agent.id, provider: model.provider, model: model.model });
      const title = '[Codex] ' + req.title;
      await this.ctx.sessionController.rename({ sessionId: agent.id, title });
      const t = { id: req.task_id, session_id: agent.id, workspace_id: workspace.id, workspace: cwd,
        provider: model.provider, model: model.model, usage_capture: 1,
        desktop_title: title, title: req.title, permission: req.permission, computer_use: computerUse, owner: 'codex', status: 'queued',
        created: stamp(), request_id: 'codex-' + req.task_id,
        ...(parent ? { parent_task_id: parent.id } : {}) };
      this.tasks.set(t.id, t); this.bySession.set(t.session_id, t.id); this.save();
      this.log(t, { type: 'native_session', session_id: t.session_id, workspace_id: t.workspace_id, title, control: t.owner });
      this.log(t, { type: 'usage_capture', version: 1 });
      try {
        if (computerUse) this.log(t, { type: 'computer_use', phase: 'available',
          text: 'Harness 官方电脑操作可用；DeepSeek 按任务需要选择工具，无桥接 GUI 次数或前后台限制。' });
        await this.ctx.sessionController.prompt({ sessionId: t.session_id, requestId: t.request_id,
          content: [{ type: 'text', text: req.task }], clientTimeZone: this.config.clientTimeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone }, new AbortController().signal);
        if (ACTIVE.has(t.status)) {
          t.status = 'running'; this.save();
          this.timers.set(t.id, setTimeout(() => this.stop(t.id, 'timed_out').catch(e => this.fail(t, e)), req.timeout_seconds * 1000));
        }
      } catch (e) { this.fail(t, e); throw e; }
      return this.publicTask(t);
    });
  }
  fail(t, error) {
    t.status = 'failed'; t.error = redact(String(error?.message ?? error)); t.finished = stamp();
    this.clear(t); this.save(); this.log(t, { type: 'error', message: t.error, trace: redact(error?.stack ?? '') });
  }
  clear(t) {
    clearTimeout(this.timers.get(t.id)); this.timers.delete(t.id);
  }
  takeover(sessionId, reason = 'client_input') {
    const t = this.tasks.get(this.bySession.get(sessionId));
    if (!t || t.owner === 'human') return t;
    t.owner = 'human'; t.handoff_reason = reason; t.handed_off = stamp();
    if (ACTIVE.has(t.status)) { t.status = 'handed_off'; t.finished = stamp(); }
    this.clear(t); this.save();
    this.log(t, { type: 'handoff', control: 'human', reason,
      text: '用户已在 Harness 接手；Codex 不再发送消息或取消用户的工作。' });
    return t;
  }
  async stop(id, status = 'cancelled') {
    const t = this.get(id);
    if (t.owner !== 'codex') throw new Error('用户已接手，不能取消用户的工作。');
    if (!ACTIVE.has(t.status)) return this.publicTask(t);
    t.stop_reason = status;
    const agent = this.ctx.agents.get(t.session_id);
    if (agent) { agent.cancel({ kind: 'user' }); await agent.whenIdle(); }
    if (t.owner !== 'codex') return this.publicTask(t);
    t.status = status; t.finished = stamp(); this.clear(t); this.save();
    return this.publicTask(t);
  }
  async handoff(id) {
    const t = this.get(id);
    // End the delegated activity before explicitly transferring an active session.
    if (t.owner === 'codex' && ACTIVE.has(t.status)) await this.stop(id);
    this.takeover(t.session_id, 'explicit_handoff');
    return this.publicTask(t);
  }
  async returnControl(id) {
    return this.serial(async () => {
      const t = this.get(id);
      if (this.bySession.get(t.session_id) !== t.id) throw new Error('请使用此会话最新的任务 ID 交回控制权。');
      if (ACTIVE.has(t.status)) throw new Error('委派仍在运行，不能交回。');
      const found = await this.ctx.sessionController.resolveAgent(t.session_id);
      if (!found.agent) throw found.error ?? new Error('原会话不可用，不能交回。');
      // Resolve may load the session. Recheck after that await; never stop human work.
      if (this.bySession.get(t.session_id) !== t.id) throw new Error('会话已有新任务，请重新检查。');
      for (const agent of this.ctx.agents.list()) if (sameTree(t.workspace, agent.session.header.cwd) &&
          (agent.status !== 'idle' || agent.inbox?.hasPending !== false))
        throw new Error('客户端仍在运行或有排队消息，请先完成或自行停止，再交回 Codex。');
      if (t.owner === 'codex') return this.publicTask(t);
      t.owner = 'codex'; t.returned_to_codex = stamp();
      this.save(); this.log(t, { type: 'control_return', control: 'codex',
        text: '用户明确交回原会话；未发送模型任务，原权限不变。' });
      return this.publicTask(t);
    });
  }
  observe(session, event) {
    const t = this.tasks.get(this.bySession.get(session.id));
    if (!t || t.owner !== 'codex' || !ACTIVE.has(t.status)) return;
    const d = event.data;
    if (event.type === 'step/start') t.usage_started = stamp();
    if (event.type === 'model/selection') { t.provider = d.provider; t.model = d.model; }
    if (['assistant/message', 'assistant/attempt'].includes(event.type) ||
        (event.type === 'compaction/summary' && d.llmStreamCall === true)) {
      const streamSample = [...(d.stream ?? [])].reverse().find(x => x.type === 'chunk' && x.chunk?.type === 'usage');
      const raw = d.usage ?? streamSample?.chunk?.usage;
      const usage = {};
      for (const key of ['inputTokens','outputTokens','totalTokens','cacheReadTokens','cacheWriteTokens','reasoningTokens'])
        if (Number.isSafeInteger(raw?.[key]) && raw[key] >= 0) usage[key] = raw[key];
      const source = d.message?.source ?? d;
      this.log(t, { type: 'usage', source: event.type, seq: event.seq, turn: d.turn, step: d.step,
        provider: source.provider ?? t.provider, model: source.model ?? t.model,
        started: event.type === 'compaction/summary' ? undefined : t.usage_started,
        usage: Object.hasOwn(usage, 'inputTokens') && Object.hasOwn(usage, 'outputTokens') ? usage : null });
    }
    if (event.type === 'assistant/message') {
      const message = d.message;
      const text = textOf(message?.content);
      if (text) { t.final_text = redact(text); this.log(t, { type: 'text', text }); }
      for (const call of message?.content ?? []) if (call.type === 'tool-call')
        this.log(t, { type: 'tool_call', call_id: call.id, name: call.name, arguments: call.arguments });
    } else if (event.type === 'tool/result') {
      this.log(t, { type: 'tool_result', call_id: d.message?.toolCallId ?? d.message?.source?.callId,
        content: d.message?.content, is_error: d.message?.isError });
    } else if (event.type === 'step/end') {
      this.log(t, { type: 'status', phase: 'step_end', step: d.step });
    } else if (event.type === 'turn/end') {
      t.status = t.stop_reason ?? (d.reason?.kind === 'completed' ? 'completed' : 'failed');
      t.finished = stamp(); t.turn_reason = d.reason; this.clear(t);
      if (t.status === 'completed') {
        fs.writeFileSync(path.join(this.config.runsDirectory, t.id, 'result.md'), t.final_text ?? '', 'utf8');
        this.log(t, { type: 'final', text: t.final_text ?? '' });
      } else if (t.status === 'failed') t.error = 'Native turn ended: ' + (d.reason?.kind ?? 'unknown');
      this.log(t, { type: 'status', phase: 'turn_end', reason: d.reason }); this.save();
    }
  }
  wire() {
    // Passive progress only: the official provider owns tools, policy, and lifetime.
    this.ctx.on('tools/execute', async (exec, next) => {
      const t = this.tasks.get(this.bySession.get(exec.agent?.id));
      if (t?.owner === 'codex' && ACTIVE.has(t.status) && exec.name.startsWith('cua_driver_native__')) {
        t.computer_calls = (t.computer_calls ?? 0) + 1;
        this.save(); this.log(t, { type: 'computer_use', phase: 'call', name: exec.name,
          calls: t.computer_calls });
      }
      return next();
    }, { global: true });
    this.ctx.on('session/event', (session, event) => this.observe(session, event), { global: true });
    this.ctx.on('agent/inbox/inserted', ({ agent, message }) => {
      const t = this.tasks.get(this.bySession.get(agent.id));
      if (t && message.source?.kind === 'user' && message.source?.rpcId !== t.request_id)
        this.takeover(agent.id);
    }, { global: true });
    this.ctx.on('agent/error', ({ agent, error }) => {
      const t = this.tasks.get(this.bySession.get(agent.id));
      if (t?.owner === 'codex' && ACTIVE.has(t.status)) this.fail(t, error);
    }, { global: true });
    this.ctx.on('agent/pre-step', async ({ agent }, next) => {
      // A human starts another session on these files: stop delegated work first.
      for (const t of this.tasks.values()) if (t.owner === 'codex' && ACTIVE.has(t.status) &&
          t.session_id !== agent.id && sameTree(agent.session.header.cwd, t.workspace)) {
        await this.stop(t.id);
        this.takeover(t.session_id, 'workspace_client_input');
      }
      return next();
    }, { global: true });
    this.ctx.commands.register({ name: 'codex-takeover', description: '接手 Codex 委派的会话，停止 Codex 继续调度',
      handler: async ({ agent }) => {
        const id = this.bySession.get(agent.id);
        if (!id) return { kind: 'error', text: '这个会话不是 Codex 委派任务。' };
        await this.handoff(id);
        return { kind: 'success', text: '你已接手。直接在本会话输入即可继续，Codex 不会再调度它。' };
      } });
    this.ctx.commands.register({ name: 'codex-return', description: '将空闲的委派会话交回 Codex；不会自动执行任务',
      handler: async ({ agent }) => {
        const id = this.bySession.get(agent.id);
        if (!id) return { kind: 'error', text: '这个会话不是 Codex 委派任务。' };
        await this.returnControl(id);
        return { kind: 'success', text: '会话已交回 Codex。下一次执行仍需你明确要求使用 DeepSeek。' };
      } });
  }
  async dispatch(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Invalid request');
    switch (input.operation) {
      case 'health': { const cua = this.computerUseState(); return { protocol: PROTOCOL, version: 1, revision: 8,
        computer_use: cua.tools_ready === true, computer_use_provider: cua.provider,
        computer_use_tools: cua.catalog_size, usage_tracking: true, prompt_signal: true, pid: process.pid,
        native_session: true, return_control: true }; }
      case 'computer_use_check': return this.computerUseState();
      case 'submit': return this.submit(input);
      case 'status': return this.publicTask(this.get(input.task_id));
      case 'cancel': return this.stop(input.task_id);
      case 'handoff': return this.handoff(input.task_id);
      case 'return': return this.returnControl(input.task_id);
      default: throw new Error('Operation not allowed');
    }
  }
  async listen() {
    this.token = randomBytes(32).toString('hex');
    this.server = http.createServer(async (req, res) => {
      const reply = (status, body) => {
        res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
          'X-Content-Type-Options': 'nosniff' }); res.end(JSON.stringify(body));
      };
      const auth = req.headers.authorization ?? '';
      const expected = 'Bearer ' + this.token;
      if (req.headers.origin || req.headers.host !== `127.0.0.1:${this.server.address().port}` ||
          Buffer.byteLength(auth) !== Buffer.byteLength(expected) || !timingSafeEqual(Buffer.from(auth), Buffer.from(expected)))
        return reply(403, { error: 'Local connector authentication required' });
      if (req.method !== 'POST' || req.url !== '/rpc' || req.headers['content-type'] !== 'application/json')
        return reply(404, { error: 'Unsupported route' });
      let bytes = 0; const chunks = [];
      try {
        for await (const chunk of req) { bytes += chunk.length; if (bytes > MAX_BODY) return reply(413, { error: 'Request too large' }); chunks.push(chunk); }
        const value = await this.dispatch(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        reply(200, value);
      } catch (e) { reply(400, { error: redact(String(e?.message ?? e)) }); }
    });
    this.server.requestTimeout = 30000; this.server.headersTimeout = 10000;
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(0, '127.0.0.1', resolve); });
    this.discovery = path.join(this.config.stateDirectory, 'connection.json');
    atomic(this.discovery, { protocol: PROTOCOL, port: this.server.address().port, token: this.token, pid: process.pid });
  }
  async close() {
    for (const t of this.tasks.values()) if (t.owner === 'codex' && ACTIVE.has(t.status))
      await this.stop(t.id).catch(e => this.fail(t, e));
    try { if (JSON.parse(fs.readFileSync(this.discovery, 'utf8')).token === this.token) fs.unlinkSync(this.discovery); } catch {}
    this.server?.closeAllConnections(); await new Promise(resolve => this.server ? this.server.close(resolve) : resolve());
  }
}

export async function install(ctx, config, policy) {
  const connector = new Connector(ctx, config, policy);
  connector.wire();
  await connector.listen();
  ctx.effect(() => () => connector.close(), 'codex-deepseek-connector');
}
