"""Preview by default. --apply installs public plugin/MCP config, never runs a model."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
BEGIN = '# BEGIN codex-deepseek managed connector'
END = '# END codex-deepseek managed connector'


def artifacts(root, profile, state, python):
    root = root.resolve()
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    patch = (f'{BEGIN}\n- insert:\n    - id: codex-deepseek-connector\n'
        '      name: ./codex-deepseek-connector.mjs\n      config:\n'
        f'        bridgeRoot: {quote(root.as_posix())}\n'
        f'        runsDirectory: {quote((root/"runs").as_posix())}\n'
        f'        stateDirectory: {quote(state.resolve().as_posix())}\n{END}\n')
    config = ('[mcp_servers.deepseek]\n' + f'command = {quote(python)}\n'
        + f'args = [{quote(str(root/"mcp_server.py"))}]\n'
        + 'startup_timeout_sec = 20\ntool_timeout_sec = 60\n')
    skill = (root/'skills/deepseek-delegate/SKILL.md').read_text(encoding='utf-8').replace('@@BRIDGE_ROOT@@',root.as_posix())
    return patch, config, skill


def replace_block(original, patch):
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise ValueError('Managed connector markers are invalid; inspect the profile manually')
        start, stop = original.index(BEGIN), original.index(END) + len(END)
        if stop < start:
            raise ValueError('Managed connector markers are out of order')
        return original[:start] + patch.rstrip() + original[stop:]
    if 'codex-deepseek-connector' in original:
        raise ValueError('An existing unmanaged connector is present; use manual migration instead of replacing it')
    return original.rstrip() + '\n\n' + patch


def protect_state(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    sid = subprocess.run(['powershell.exe','-NoProfile','-Command',
        '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
        check=True, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'S-1-5-\d+(?:-\d+)+', sid):
        raise ValueError('Could not resolve current Windows SID')
    subprocess.run(['icacls.exe',str(state),'/inheritance:r','/grant:r',
        f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'],
        check=True, capture_output=True)


def apply(root, profile, codex_home, state, python, *, secure_state=protect_state):
    profile_file, config_file = profile/'cordis.patch.yml', codex_home/'config.toml'
    if not profile_file.is_file():
        raise ValueError('Start Harness once first, or specify --profile pointing to an existing desktop profile')
    patch, config, skill = artifacts(root, profile, state, python)
    old_profile = profile_file.read_text(encoding='utf-8-sig')
    new_profile = replace_block(old_profile, patch)
    old_config = config_file.read_text(encoding='utf-8-sig') if config_file.exists() else ''
    parsed = tomllib.loads(old_config)
    if 'deepseek' in parsed.get('mcp_servers', {}):
        raise ValueError('mcp_servers.deepseek already exists; inspect it manually, it will not be overwritten')
    skill_dir = codex_home/'skills/deepseek-delegate'
    if skill_dir.exists():
        raise ValueError('deepseek-delegate skill already exists; inspect it manually, it will not be overwritten')
    # Validate all targets before writes. Back up existing files before modifying them.
    suffix = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    destinations = {
        profile_file:new_profile,
        profile/'codex-deepseek-connector.mjs':(root/'harness/connector.mjs').read_text(encoding='utf-8'),
        config_file:old_config.rstrip() + '\n\n' + config,
        skill_dir/'SKILL.md':skill,
        skill_dir/'agents/openai.yaml':(root/'skills/deepseek-delegate/agents/openai.yaml').read_text(encoding='utf-8')}
    snapshots = {p:p.read_bytes() if p.exists() else None for p in destinations}
    secure_state(state)
    changed = []
    try:
        for target, text in destinations.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            if snapshots[target] is not None:
                shutil.copy2(target, target.with_name(target.name + '.backup-' + suffix))
            changed.append(target)
            target.write_text(text, encoding='utf-8')
    except Exception:
        for target in reversed(changed):
            if snapshots[target] is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(snapshots[target])
        raise
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Explicitly install after reviewing generated files')
    parser.add_argument('--profile',type=Path,default=Path.home()/'.dsh/profiles/desktop')
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    parser.add_argument('--state-directory',type=Path,default=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'CodexDeepSeek/desktop')
    parser.add_argument('--output',type=Path,default=ROOT/'generated')
    args = parser.parse_args()
    patch, config, skill = artifacts(ROOT,args.profile,args.state_directory,sys.executable)
    args.output.mkdir(parents=True,exist_ok=True)
    for name, text in [('harness.patch.yml',patch),('codex-mcp.toml',config),('SKILL.md',skill)]:
        (args.output/name).write_text(text,encoding='utf-8')
    print('Preview written to',args.output.resolve())
    if not args.apply:
        print('No Harness/Codex profile was changed. Review the files, then run with --apply.')
        return
    if os.name != 'nt':
        raise ValueError('This release supports Windows desktop installation only')
    for target in apply(ROOT,args.profile,args.codex_home,args.state_directory,sys.executable):
        print('Installed',target)
    print('Fully quit and reopen Harness, then restart Codex. No client or model was launched.')


if __name__ == '__main__':
    try:
        main()
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print('Installation stopped:',error,file=sys.stderr)
        sys.exit(1)
