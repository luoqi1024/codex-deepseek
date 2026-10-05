"""Local, file-backed Codex -> DeepSeek Harness task bridge. Python stdlib only."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import assignment_policy as policy
import usage_meter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
DSH = Path(os.environ.get('CODEX_DEEPSEEK_DSH') or shutil.which('dsh.cmd') or shutil.which('dsh') or ROOT / 'dsh-not-configured')
PROFILE = "codex-deepseek"
PORT = 47831
TEMP_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "CodexDeepSeek" / "temp"

@contextmanager
def submission_lock(wait_seconds=0):
    # Serialize independent Codex chats between the active-task check and spawn.
    with (ROOT / ".submit.lock").open("a+b") as lock:
        lock.seek(0)
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Another task is being submitted; retry shortly") from exc
                    time.sleep(0.02)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)

def stamp():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")

def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback

def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)

def run_path(run_id):
    if not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", run_id):
        raise ValueError("Invalid task id")
    path = RUNS / run_id
    if not path.is_dir():
        raise ValueError("Task not found")
    return path

# Redact familiar credential forms if a tool happens to echo them. Never read key stores.
def scrub(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.fullmatch(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)", k) else scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    return re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)([\"']?)[^\s,\"']+", r"\1[REDACTED]", value)

def event(path, payload, filename="events.jsonl"):
    if payload.get("type") == "thinking":
        return
    record = {"time": stamp(), "order": time.time_ns(), **scrub(payload)}
    with (path / filename).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def lines(path):
    result = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # An in-flight final line is returned on the next poll.
    except FileNotFoundError:
        pass
    return result

def worker_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        # Access denied is not evidence of exit.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)

def current_state(path):
    state = read_json(path / "state.json", {})
    active = state.get("status") in ("queued", "running")
    age = time.time() - dt.datetime.fromisoformat(state["created"]).timestamp() if active else 0
    monitor_missing = active and age > 30 and (not state.get("worker_pid") or not worker_alive(state["worker_pid"]))
    desktop = state.get("backend") == "desktop"
    state["native_status_verified"] = False
    state["native_task_missing"] = False
    reconciled = False
    if desktop and state.get("id"):
        try:
            import desktop_client
            live = desktop_client.call("status", timeout=2, task_id=state["id"])
            if not isinstance(live, dict):
                raise RuntimeError("Invalid native task status")
            if live.get("status") in ("queued", "running", "completed", "failed", "cancelled", "timed_out", "handed_off"):
                reconciled = state.get("status") != live["status"]
                if state.get("error") and state.get("status") != live["status"]:
                    state["monitor_error"] = state.pop("error")
                if live["status"] in ("queued", "running"):
                    state.pop("finished", None)
                    state["exit_code"] = None
                elif live["status"] == "completed":
                    state["exit_code"] = 0
                state.update({k: live[k] for k in ("status", "session_id", "workspace_id", "desktop_title", "finished", "error", "turn_reason") if k in live})
                state.update(native_status_verified=True, execution_uncertain=False, native_task_missing=False)
            state.update({k: live[k] for k in ("control", "owner", "handed_off", "handoff_reason", "returned_to_codex") if k in live})
            state["desktop_connection"] = "connected"
        except RuntimeError as exc:
            if str(exc) == "Task not managed by this connector":
                state.update(desktop_connection="connected", native_task_missing=True)
            else:
                state["desktop_connection"] = "unavailable"  # Preserve readable history without claiming live state.
    # Upgrade the two initial runs: this Harness returns {kind:'completed'},
    # while the first bridge draft expected a scalar string.
    if state.get("profile") == PROFILE and state.get("status") == "failed" and state.get("exit_code") == 0 and not state.get("error") and not state.get("stream_contract"):
        records = lines(path / "events.jsonl")
        if any(e.get("type") == "final" for e in records) and any(e.get("phase") == "turn_end" and e.get("reason") == {"kind": "completed"} for e in records):
            state.update(status="completed", stream_contract=2)
            write_json(path / "state.json", state)
            event(path, {"type": "bridge_status", "status": "completed"})
    if monitor_missing:
        if desktop:
            state["monitor_detached"] = True
            if not state["native_status_verified"]:
                state["execution_uncertain"] = True
            write_json(path / "state.json", state)
        else:
            state.update(status="failed", finished=stamp(), error="Worker exited unexpectedly; inspect existing files before retrying.")
            write_json(path / "state.json", state)
    elif desktop and reconciled and (not state.get("worker_pid") or not worker_alive(state["worker_pid"])):
        state["monitor_detached"] = True
        write_json(path / "state.json", state)
    # A worker finishing its final state write can race with local review.
    # Keep the independently stored review authoritative for display as well.
    review = read_json(path/'review.json', {}) or {}
    if (isinstance(review, dict) and review.get('task_id') == state.get('id')
            and review.get('contract_hash') == state.get('contract_hash') and review.get('verdict') in policy.VERDICTS):
        state['review'] = review['verdict']
    state['assignment'] = policy.summary(RUNS, state)
    state.update(task_view(state))
    return state


def task_view(state):
    """User-facing projection; raw worker status and ownership remain independent."""
    status = state.get("status")
    control = state.get("control") or state.get("owner") or "codex"
    control_label = "用户" if control == "human" else "Codex"
    if state.get("native_task_missing"):
        stage, label, action = "native_task_missing", "原任务待核对", "连接器未找到原任务映射，当前为历史记录；先在 Harness 原对话核对，不要重新提交。"
    elif state.get("execution_uncertain"):
        stage, label, action = "execution_uncertain", "执行状态待确认", "监控已中断，Harness 可能仍在执行；先恢复连接并检查原任务，不要重新提交。"
    elif state.get("desktop_connection") == "unavailable":
        stage, label, action = "connection_unavailable", "连接待恢复", "检查 Harness 是否运行；当前显示历史状态，不要重复提交任务。"
    elif control == "human":
        stage, label, action = "human_control", "用户已接手", "在 Harness 原对话中继续；需要交回时明确告诉 Codex。"
    elif status in ("queued", "running"):
        stage, label, action = status, "排队中" if status == "queued" else "执行中", "等待关键进度；接手可先停止委派。"
    elif state.get("returned_to_codex"):
        stage, label, action = "returned_to_codex", "已交回 Codex", "尚未执行新任务；继续使用 DeepSeek 需要明确要求。"
    elif state.get('assignment', {}).get('policy') == 'bounded' and state['assignment']['attempts_remaining'] == 0 and state.get('review') in ('changes-needed', 'inconclusive'):
        stage, label, action = "attempts_exhausted", "执行次数已用完", "两轮执行已用完；报告剩余问题与已有成果，停止自动派发，不为可选改善重做。"
    elif state.get("review") == "inconclusive":
        stage, label, action = "review_inconclusive", "验收尚无法确认", "先补充实际检查；未核实不等于失败，不为主观偏好返工。"
    elif status in ("failed", "timed_out", "error") or state.get("review") == "changes-needed":
        stage, label, action = "needs_attention", "需要处理", "查看错误或验收批注，先检查已有文件，再决定下一步。"
    elif status == "completed":
        stage, label, action = "accepted" if state.get("review") == "accepted" else "awaiting_review", "验收通过" if state.get("review") == "accepted" else "待 Codex 验收", "按原任务目标决定后续工作。" if state.get("review") == "accepted" else "由 Codex 核对实际结果；执行结束不等于验收通过。"
    elif status == "cancelled":
        stage, label, action = "cancelled", "已取消", "检查已经完成的改动，再决定继续或接手。"
    else:
        stage, label, action = "unknown", "状态待确认", "先检查任务记录，避免重复执行。"
    return {"stage": stage, "stage_label": label, "control_label": control_label, "next_action": action}


def request_cancel(path, state=None):
    state = state or current_state(path)
    if state.get("status") not in ("queued", "running"):
        return state, False
    if state.get("backend") == "desktop":
        if state.get("control") == "human" or state.get("owner") == "human":
            raise RuntimeError("用户已接手，不能取消用户的工作。")
        import desktop_client
        live = desktop_client.call("cancel", timeout=45, task_id=state["id"])
        state.update({k:v for k,v in live.items() if k not in ("id", "created", "final_text")})
        state["execution_uncertain"] = False
        write_json(path / "state.json", state)
    else:
        (path / "cancel.request").touch()
    return state, True


def desktop_prompt(path, state):
    instructions = (ROOT / "worker-instructions.txt").read_text(encoding="utf-8")
    reminder_file = ROOT / "worker-reminder.txt"
    # Legacy installations or incomplete updates keep the full rules.
    reminder = reminder_file.read_text(encoding="utf-8") if reminder_file.is_file() else instructions
    policy_hash = hashlib.sha256(("desktop-policy-v1\0" + instructions + "\0" + reminder).encode("utf-8")).hexdigest()
    parent = {}
    if state.get("parent_task_id") and state.get("resume_session"):
        try:
            parent = read_json(run_path(state["parent_task_id"]) / "state.json", {}) or {}
            if not isinstance(parent, dict):
                parent = {}
        except (ValueError, OSError):
            pass
    reuse_rules = (parent.get("status") == "completed" and parent.get("backend") == "desktop"
                   and parent.get("profile") == PROFILE and parent.get("session_id") == state.get("resume_session")
                   and parent.get("workspace") == state.get("workspace")
                   and parent.get("prompt_policy_hash") == policy_hash)
    mode = "brief" if reuse_rules else "full"
    rules = reminder if reuse_rules else instructions
    task = (path / "task.txt").read_text(encoding="utf-8")
    state.update(prompt_mode=mode, prompt_policy_hash=policy_hash)
    contract = policy.prompt(RUNS, state) if state.get('assignment_id') else ''
    return rules + contract + "\n\n--- 本次任务 ---\n" + task

def snapshot(path):
    value = current_state(path)
    events = lines(path / "events.jsonl") + lines(path / "notes.jsonl") + lines(path / "diagnostics.jsonl")
    events.sort(key=lambda x: x["order"])
    metering = task_usage(path, value)
    return {**value, "events": events, "usage": metering['tokens'], 'metering': metering}


def task_usage(path, state):
    settings = read_json(ROOT / 'settings.json', {}) or {}
    plan = settings.get('usage_plan')
    result = usage_meter.summarize(path, state, plan)
    if state.get('assignment_id'):
        try:
            _, _, budget = policy.load(RUNS, state)
        except ValueError:
            return result
        values = []
        for attempt in budget['attempts']:
            item_path = run_path(attempt['task_id'])
            item_state = read_json(item_path/'state.json', {}) or {}
            values.append(usage_meter.summarize(item_path, item_state, plan))
        result['assignment_total'] = usage_meter.combine(values, plan)
    return result

def ensure_server():
    import urllib.request
    url = f"http://127.0.0.1:{PORT}"
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
            if json.load(response).get("service") == "codex-deepseek":
                return url
    except Exception:
        pass
    subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "serve"], cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    for _ in range(30):
        time.sleep(0.2)
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                if json.load(response).get("service") == "codex-deepseek":
                    return url
        except Exception:
            pass
    raise RuntimeError(f"Dashboard cannot start; check port {PORT}")

def submit(args):
    workspace = Path(args.workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    task = Path(args.task_file).read_text(encoding="utf-8-sig")
    if not task.strip():
        raise ValueError("Empty task")
    session_id = getattr(args, "session_id", None)
    if session_id and not re.fullmatch(r"session-[a-f0-9-]{36}", session_id):
        raise ValueError("Resume only a session UUID returned by this bridge")
    backend = getattr(args, "backend", None) or read_json(ROOT / "settings.json", {}).get("backend", "headless")
    if backend not in ("desktop", "headless"):
        raise ValueError("Invalid execution backend")
    if backend == 'headless' and not DSH.is_file():
        raise RuntimeError('Set CODEX_DEEPSEEK_DSH to the Harness CLI executable for headless mode')
    if backend == "desktop":
        import desktop_client
        connection = desktop_client.check_connection()
        if not connection["ready"]:
            raise RuntimeError(scrub(connection["message"]) + " " + connection["next_action"])
    # One writer at a time per workspace. Stale workers are handled separately.
    for old in RUNS.glob("*/state.json"):
        state = current_state(old.parent)
        old_workspace = Path(state.get("workspace", str(RUNS))).resolve()
        if state.get("status") in ("queued", "running") and (workspace.is_relative_to(old_workspace) or old_workspace.is_relative_to(workspace)):
            raise RuntimeError(f"Workspace overlaps active task {state['id']}; check or cancel it first")
    parent_id = getattr(args, 'parent_task_id', None)
    parent = current_state(run_path(parent_id)) if parent_id else None
    if parent:
        if (parent.get('status') not in ('completed', 'failed', 'cancelled', 'timed_out', 'handed_off')
                or parent.get('profile') != PROFILE or parent.get('backend', 'headless') != backend
                or Path(parent.get('workspace', '')).resolve() != workspace
                or parent.get('session_id') != session_id or not session_id
                or parent.get('permission') not in ('read-only', 'workspace-write')
                or parent.get('permission') == 'read-only' and args.permission != 'read-only'):
            raise ValueError('父任务状态、工作区、会话或权限不匹配，拒绝续接。')
        if parent.get('control') == 'human' or parent.get('owner') == 'human':
            raise ValueError('用户已接手，不得续接用户工作。')
        if backend == 'desktop' and not parent.get('native_status_verified'):
            raise ValueError('尚未确认原生任务已停止，先检查恢复说明。')
    elif session_id:
        raise ValueError('复用会话必须提供本桥接管理的父任务。')
    plan = policy.prepare(RUNS, parent, getattr(args, 'contract', None), getattr(args, 'repair_reason', None))
    # MCP callers skip the dashboard so no browser/server is started for them;
    # the fixed URL stays recorded in state for an explicit `details` call.
    url = f"http://127.0.0.1:{PORT}" if getattr(args, "no_dashboard", False) else ensure_server()
    run_id = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    path = RUNS / run_id
    path.mkdir(parents=True)
    (path / "task.txt").write_text(task, encoding="utf-8")
    state = {"id": run_id, "title": args.title, "workspace": str(workspace), "created": stamp(),
        "status": "queued", "permission": args.permission, "timeout_seconds": args.timeout,
        "resume_session": session_id, "profile": PROFILE,
        "provider": "opencode-go", "model": "deepseek-v4.1-flash", "dashboard": url + "/?run=" + run_id}
    state["backend"] = backend
    if parent_id:
        state["parent_task_id"] = parent_id
    policy.reserve(plan, path, state, write_json)
    write_json(path / "state.json", state)
    event(path, {"type": "codex", "text": task}, "notes.jsonl")
    event(path, {'type': 'codex', 'phase': 'assignment', 'text': policy.prompt(RUNS, state)}, 'notes.jsonl')
    if parent_id:
        event(path, {"type": "bridge", "phase": "continuation", "parent_task_id": parent_id})
    try:
        proc = subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "worker", run_id], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, RuntimeError) as exc:
        state.update(status='failed', finished=stamp(), error=scrub(str(exc)), admission_uncertain=True)
        write_json(path/'state.json',state)
        event(path, {'type':'error','message':str(exc)})
        raise RuntimeError(f'任务 {run_id} 启动失败，执行预留保留；先检查恢复说明，不重新提交。{scrub(str(exc))}') from None
    print(json.dumps({"id": run_id, "worker_pid": proc.pid, "dashboard": state["dashboard"], "state_file": str(path / "state.json")}, ensure_ascii=False))
    return {"id": run_id, "worker_pid": proc.pid, "dashboard": state["dashboard"],
        "state_file": str(path / "state.json"), "title": state["title"], "status": state["status"],
        "workspace": state["workspace"], "permission": state["permission"],
        "timeout_seconds": state["timeout_seconds"], "parent_task_id": parent_id,
        "session_id": session_id}


def record_review(path, text, verdict, checks=None, suggestions=None):
    with submission_lock():
        state = current_state(path)
        if state.get('status') in ('queued', 'running'):
            raise ValueError('任务仍在运行，停止前不能给出验收结论')
        if verdict not in policy.VERDICTS:
            raise ValueError('verdict 必须为 accepted、changes-needed 或 inconclusive')
        if state.get('assignment_id'):
            _, contract, _ = policy.load(RUNS, state)
            review = policy.validate_review(contract, verdict, checks, suggestions)
        else:
            if checks is not None or suggestions is not None:
                raise ValueError('旧任务没有固定条件，仅允许记录历史批注。')
            review = {'verdict': verdict, 'checks': [], 'suggestions': []}
        review.update(task_id=state['id'], contract_hash=state.get('contract_hash'), text=text, recorded_at=stamp())
        review = scrub(review)
        write_json(path / 'review.json', review)
        note = text
        for check in review['checks']:
            note += f"\n{check['criterion_id']} {check['outcome']}：{check['evidence']}"
        if review['suggestions']:
            note += '\n可选建议（不阻止通过）：' + '；'.join(review['suggestions'])
        event(path, {'type':'codex', **review, 'text':note}, 'notes.jsonl')
        state['review'] = verdict
        write_json(path / 'state.json', state)
        return state, review

def desktop_worker(run_id):
    import desktop_client
    path = run_path(run_id)
    state = read_json(path / "state.json")
    state.update(status="running", started=stamp(), worker_pid=os.getpid(), backend="desktop")
    write_json(path / "state.json", state)
    try:
        prompt = desktop_prompt(path, state)
        native = desktop_client.call("submit", timeout=45, task_id=run_id,
            workspace=state["workspace"], permission=state["permission"], title=state["title"],
            timeout_seconds=state["timeout_seconds"], task=prompt,
            parent_task_id=state.get("parent_task_id"), session_id=state.get("resume_session"))
        while True:
            # Never kill the desktop host, and never cancel a session after human takeover.
            current = read_json(path / "state.json", {})
            for key in ("review",):
                if key in current: state[key] = current[key]
            state.update({k: v for k, v in native.items() if k not in ("id", "created", "final_text")})
            write_json(path / "state.json", state)
            if native["status"] not in ("queued", "running"):
                break
            if (path / "cancel.request").exists():
                try:
                    native = desktop_client.call("cancel", timeout=45, task_id=run_id)
                except RuntimeError:
                    # Client input may have transferred ownership since the last poll.
                    native = desktop_client.call("status", task_id=run_id)
                    if native.get("control") != "human":
                        raise
            else:
                time.sleep(0.8)
                native = desktop_client.call("status", task_id=run_id)
        state["exit_code"] = 0 if state["status"] == "completed" else None
    except Exception as exc:
        state.update(status="failed", error=scrub(str(exc)))
        event(path, {"type": "error", "message": str(exc)})
    finally:
        state.update(finished=stamp(), stream_contract=3)
        write_json(path / "state.json", state)
        event(path, {"type": "bridge_status", "status": state["status"]})

def worker(run_id):
    state = read_json(run_path(run_id) / "state.json", {})
    # Also handles submissions from a pre-reload MCP server: the new worker is a fresh process.
    backend = state.get("backend") or read_json(ROOT / "settings.json", {}).get("backend", "headless")
    if backend == "desktop":
        return desktop_worker(run_id)
    return headless_worker(run_id)

def headless_worker(run_id):
    path = run_path(run_id)
    state = read_json(path / "state.json")
    state.update(status="running", started=stamp(), worker_pid=os.getpid())
    write_json(path / "state.json", state)
    stop = threading.Event()
    output_error = []
    proc = None
    def consume(stream, diagnostic=False):
        try:
            for line in stream:
                text = line.rstrip()
                if not text:
                    continue
                if diagnostic:
                    event(path, {"type": "diagnostic", "text": text}, "diagnostics.jsonl")
                else:
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        payload = {"type": "diagnostic", "text": text}
                    if payload.get("type") == "session":
                        state["session_id"] = payload.get("sessionId")
                        write_json(path / "state.json", state)
                    if payload.get("type") == "final":
                        (path / "result.md").write_text(scrub(payload.get("text", "")), encoding="utf-8")
                    event(path, payload)
        except Exception as exc:
            output_error.append(str(exc))

    def terminate():
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            else:
                proc.terminate()
    try:
        # Only permission differences are per invocation. Base profile retains workspace confinement.
        permission = state["permission"]
        patch = [{"id": "permission", "config": {"presets": {"codex-task": {"sandbox": permission, "approval": "never"}}, "defaultPreset": "codex-task"}},
                 {"id": "sandbox-policy", "config": {"mode": permission, "workspaceRoot": state["workspace"]}},
                 {"id": "approval", "config": {"policy": "never"}}]
        (path / "invocation.patch.yml").write_text(json.dumps(patch), encoding="utf-8")
        args = [str(DSH), "--profile", PROFILE, "--patch", str(path / "invocation.patch.yml"), "--json"]
        if state.get("resume_session"):
            args.extend(["--session-id", state["resume_session"]])
        # cmd receives only controlled launcher/options; the entire task is delivered through stdin.
        command = subprocess.list2cmdline(args)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # The machine's global TEMP is a legacy LLM directory whose ACL cannot
        # materialize Harness grants. Use fresh, owned per-task temp storage.
        private_temp = TEMP_ROOT / run_id
        private_temp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = env["TMP"] = str(private_temp)
        proc = subprocess.Popen(command, cwd=state["workspace"], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        state["harness_pid"] = proc.pid
        write_json(path / "state.json", state)
        readers = [threading.Thread(target=consume, args=(proc.stdout,), daemon=True), threading.Thread(target=consume, args=(proc.stderr, True), daemon=True)]
        for reader in readers:
            reader.start()
        task = (path / "task.txt").read_text(encoding="utf-8")
        instructions = (ROOT / "worker-instructions.txt").read_text(encoding="utf-8")
        contract = policy.prompt(RUNS, state) if state.get('assignment_id') else ''
        proc.stdin.write(instructions + contract + "\n\n--- Codex task ---\n" + task)
        proc.stdin.close()
        deadline = time.monotonic() + state["timeout_seconds"]
        outcome = None
        while proc.poll() is None:
            if (path / "cancel.request").exists():
                outcome = "cancelled"
                terminate()
                break
            if time.monotonic() > deadline:
                outcome = "timed_out"
                terminate()
                break
            time.sleep(0.5)
        proc.wait(timeout=20)
        for reader in readers:
            reader.join(timeout=10)
        events = lines(path / "events.jsonl")
        final_seen = any(e.get("type") == "final" for e in events)
        completed = any(e.get("type") == "status" and e.get("phase") == "turn_end" and (e.get("reason", {}).get("kind") if isinstance(e.get("reason"), dict) else e.get("reason")) == "completed" for e in events)
        state.update(status=outcome or ("completed" if proc.returncode == 0 and final_seen and completed and not output_error else "failed"), exit_code=proc.returncode)
        if output_error:
            state["error"] = "; ".join(output_error)
    except Exception as exc:
        terminate()
        state.update(status="failed", error=scrub(str(exc)))
        event(path, {"type": "error", "message": str(exc)})
    finally:
        if proc:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    stream.close()
        state.update(finished=stamp(), stream_contract=2)
        write_json(path / "state.json", state)
        event(path, {"type": "bridge_status", "status": state["status"]})

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self.send_error(403)
            return
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = (ROOT / "dashboard.html").read_bytes()
                mime = "text/html; charset=utf-8"
            elif parsed.path == "/api/health":
                body = json.dumps({"service": "codex-deepseek", "version": 1}).encode()
                mime = "application/json"
            elif parsed.path == "/api/runs":
                states = [current_state(p.parent) for p in RUNS.glob("*/state.json")]
                states.sort(key=lambda x: x.get("created", ""), reverse=True)
                body = json.dumps(states, ensure_ascii=False).encode("utf-8")
                mime = "application/json; charset=utf-8"
            elif parsed.path == "/api/run":
                run_id = parse_qs(parsed.query).get("id", [""])[0]
                body = json.dumps(snapshot(run_path(run_id)), ensure_ascii=False).encode("utf-8")
                mime = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, FileNotFoundError):
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

def main():
    RUNS.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("submit")
    start.add_argument("--workspace", required=True)
    start.add_argument("--task-file", required=True)
    start.add_argument("--title", required=True)
    start.add_argument("--timeout", type=int, default=1200)
    start.add_argument("--permission", choices=["read-only", "workspace-write"], default="workspace-write")
    start.add_argument("--session-id")
    start.add_argument("--parent-task-id")
    start.add_argument('--contract-file', help='首次执行的 JSON：acceptance_criteria、change_scope、optional_improvements。')
    start.add_argument('--repair-reason', help='第二轮的具体问题和修正办法。')
    start.add_argument("--backend", choices=["desktop", "headless"])
    start.add_argument("--no-dashboard", action="store_true", help="Do not start the dashboard for this submission (MCP tasks).")
    work = sub.add_parser("worker")
    work.add_argument("id")
    sub.add_parser("serve")
    sub.add_parser("dashboard")
    for action in ["status", "result", "cancel", "note"]:
        child = sub.add_parser(action)
        child.add_argument("id")
        if action == "note":
            child.add_argument("--text-file", required=True)
            child.add_argument("--verdict", choices=policy.VERDICTS)
            child.add_argument('--checks-file', help='JSON：checks 数组和可选 suggestions 数组。')
    args = parser.parse_args()
    if args.command == "submit":
        if not 10 <= args.timeout <= 7200:
            raise ValueError("Timeout must be 10..7200 seconds")
        args.contract = json.loads(Path(args.contract_file).read_text(encoding='utf-8-sig')) if args.contract_file else None
        with submission_lock():
            submit(args)
    elif args.command == "worker":
        path = run_path(args.id)
        # The spawning submit briefly owns this lock; wait for it, then claim
        # the queued reservation exactly once before any model operation.
        with submission_lock(wait_seconds=5):
            state = read_json(path / 'state.json', {})
            try:
                policy.verify_worker(RUNS, state)
            except ValueError as exc:
                if state.get('status') == 'queued':
                    state.update(status='failed', finished=stamp(), worker_pid=os.getpid(), error=scrub(str(exc)))
                    write_json(path/'state.json',state)
                    event(path, {'type':'error', 'message':str(exc)})
                raise
            state.update(status='running', started=stamp(), worker_pid=os.getpid())
            write_json(path/'state.json',state)
        worker(args.id)
    elif args.command == "serve":
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    elif args.command == "dashboard":
        print(ensure_server())
    else:
        path = run_path(args.id)
        if args.command == "status":
            info = snapshot(path)
            info["event_count"] = len(info.pop("events"))
            print(json.dumps(info, ensure_ascii=False))
        elif args.command == "result":
            result = path / "result.md"
            print(result.read_text(encoding="utf-8") if result.exists() else "No final result yet. Check status.")
        elif args.command == "cancel":
            state, requested = request_cancel(path)
            print(json.dumps({"cancel_requested":requested,"status":state.get("status")},ensure_ascii=False))
        elif args.command == "note":
            text = Path(args.text_file).read_text(encoding='utf-8-sig')
            if args.verdict:
                values = json.loads(Path(args.checks_file).read_text(encoding='utf-8-sig')) if args.checks_file else {}
                record_review(path, text, args.verdict, values.get('checks'), values.get('suggestions'))
            else:
                event(path, {'type':'codex', 'text':text}, 'notes.jsonl')
            print("Codex review note recorded.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": scrub(str(exc))}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
