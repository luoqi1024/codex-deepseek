"""Narrow authenticated connection to the installed Harness Cordis plugin."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import urllib.request
import urllib.error

STATE = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'CodexDeepSeek' / 'desktop'
PROTOCOL = 'codex-deepseek-desktop-v1'


def check_connection():
    """Read-only local health; readiness never implies model/account validation."""
    try:
        health = call('health', timeout=3)
    except RuntimeError as exc:
        return {'connection_state':'unavailable', 'connected':False, 'ready':False,
                'message':str(exc),
                'next_action':'打开 Harness 并保持后台运行；若已经运行，检查连接器是否启用或重开 Harness。'}
    if not isinstance(health, dict) or health.get('protocol') != PROTOCOL or health.get('version') != 1:
        return {'connection_state':'incompatible','connected':True,'ready':False,
                'message':'收到不兼容的连接器响应。',
                'next_action':'确认安装的是本机 Codex→DeepSeek 连接器，重开 Harness 后再检查。'}
    revision = health.get('revision')
    ready = (type(revision) is int and revision >= 5 and health.get('native_session') is True
             and health.get('return_control') is True)
    return {'connection_state':'ready' if ready else 'upgrade_needed','connected':True,'ready':ready,
            'connector_revision':revision if type(revision) is int else None,
            'native_sessions':health.get('native_session') is True,
            'return_control':health.get('return_control') is True,
            'usage_tracking':health.get('usage_tracking') is True,
            'message':'本机连接正常，原生会话及双向交接接口可用。' if ready else '本机连接正常，连接器尚未加载当前所需功能。',
            'next_action':'明确要求使用 DeepSeek 后才会执行任务。' if ready else '彻底退出并重开 Harness，加载已安装的新版连接器。'}

def call(operation, *, timeout=12, **arguments):
    try:
        connection = json.loads((STATE / 'connection.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Harness 桌面连接器未就绪。请打开或重启 DeepSeek Harness；不会自动打开窗口或改用其他模型。') from exc
    port, token = connection.get('port'), connection.get('token')
    if connection.get('protocol') != PROTOCOL or type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
        raise RuntimeError('Invalid desktop connector discovery record')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/rpc',
        data=json.dumps({'operation': operation, **arguments}, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
    # Ignore environment proxies: this capability stays on loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read(2 * 1024 * 1024).decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try: error = json.loads(exc.read(4096).decode('utf-8')).get('error', 'Desktop operation rejected')
        except (ValueError, UnicodeError): error = 'Desktop operation rejected'
        raise RuntimeError(error) from None
    except (OSError, ValueError) as exc:
        raise RuntimeError('Harness 桌面连接断开。检查任务状态后重试；不会重复发送任务。') from exc
    return value
