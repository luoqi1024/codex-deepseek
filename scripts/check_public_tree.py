"""Check tracked release paths and credential-shaped literals without printing secrets."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
BLOCKED_DIRS={'runs','backups','reference','generated','.temp','__pycache__','.venv','node_modules'}
BLOCKED_FILES={'connection.json','tasks.json','.env','settings.local.json','.submit.lock'}
PATTERNS=[
    re.compile(r'gh[opusr]_[A-Za-z0-9]{24,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{30,}'),
    re.compile(r'sk-[A-Za-z0-9_-]{24,}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'(?i)[A-Z]:[/\\]Users[/\\][A-Za-z0-9_.-]+[/\\]'),
]


def violations(name,data):
    parts=Path(name).parts
    errors=[]
    if set(parts)&BLOCKED_DIRS or Path(name).name in BLOCKED_FILES or Path(name).name.startswith('.env.'):
        errors.append('private/runtime path')
    if '\x00' in data:errors.append('unexpected binary file')
    for pattern in PATTERNS:
        if pattern.search(data):errors.append('credential/private-path pattern')
    return errors


def main():
    # Scan staged blobs, exactly the initial commit candidate, rather than other local files.
    names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode('utf-8').split('\0')
    errors=[];count=0
    for name in filter(None,names):
        count+=1
        data=subprocess.check_output(['git','show',':'+name],cwd=ROOT).decode('utf-8')
        errors.extend((name,reason) for reason in violations(name,data))
    for name,reason in errors:print(name+': '+reason,file=sys.stderr)
    if errors:sys.exit(1)
    print(f'Checked {count} staged text files; no blocked paths or credential-shaped literals.')


if __name__=='__main__':main()
