"""Offline secret gate for tracked files, staged content, or reachable Git history.

Print paths/rules only, never matched values. This is a targeted baseline, not a
guarantee of absence; keep GitHub secret scanning/push protection enabled too.
"""
import argparse
import json
import hashlib
from pathlib import Path
import re
import subprocess

# Exact URL fingerprints of reviewed documentation/negative-test placeholders only.
TEST_URL_DIGESTS = {
    "README.md": [
        "5532a6eedbd6961409a61a183707c1f62509ea32ae12f0fad45de323ed9dac5f"
    ],
    "test_concurrency_rehearsal.py": [
        "ed2402b59cb2949775f5f77f624fdb563855a580004dd3c81deede22f550f184"
    ],
    "test_database_config.py": [
        "93136846a4d64cbfeec09e4b1d4f242830f273f8dd02f2010f1ae949b6155618"
    ],
    "test_postgresql_migration.py": [
        "103a9f9248043c10c173f2334c355c0363b3b4a2894dffb856b9a331b90e6b1b",
        "387d2b5e2fc22004578f9edef918c7565d41ff64852a2391e04e3f0fe67a5787",
        "4b76310044bcb8f35ce12ba4e6d8818f2617dda424f56ddaba58a40875adc33f"
    ],
    "test_staging_postgres.py": [
        "b19da11589753bd77f5146ac7f758d408c4f84ffd9347f8d8782a5367d6b2199"
    ]
}

PATTERNS = {
    'wecom-webhook': rb'https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9_-]{16,}',
    'private-key': rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
    'github-token': rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})',
    'cloud-access-key': rb'(?:AKIA[A-Z0-9]{16}|AKID[A-Za-z0-9]{28,40})',
    'wechat-secret': rb'(?i)(?:app[_-]?secret|WECHAT_MINIPROGRAM_APP_SECRET)[\x20\t\x22\x27:=]+[a-f0-9]{32}\b',
    'credential-url': rb'(?i)(?:postgres(?:ql)?(?:\+\w+)?|mysql|redis)://[^\s:/]+:([^\s@/]+)@',
}


def inspect(path, data):
    hits = []
    name = Path(path).name.lower()
    if (name in {'backup-wecom-url.txt', '.env', 'server-config.json', 'id_rsa', 'id_ed25519'} or
            name.startswith('.env.') and name not in {'.env.example', '.env.sample'} or
            name.endswith(('.key', '.pem', '.p12', '.pfx')) or
            name.startswith('admin-captcha') and name.endswith('.json')):
        hits.append('private-file')
    for rule, pattern in PATTERNS.items():
        for match in re.finditer(pattern, data):
            if rule == 'credential-url' and hashlib.sha256(match.group()).hexdigest() in TEST_URL_DIGESTS.get(path, []):
                continue
            hits.append(rule)
            break
    return sorted(set(hits))


def scan(root, mode):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    if mode == 'history':
        names = {}
        for row in git('rev-list', '--objects', '--all').decode().splitlines():
            oid, _, path = row.partition(' ')
            names[oid] = path
        candidates = []
        for oid, path in names.items():
            if path and git('cat-file', '-t', oid).strip() == b'blob':
                candidates.append((oid, path))
    else:
        candidates = []
        for row in git('ls-files', '--stage', '-z').split(b'\0'):
            if not row: continue
            meta, path = row.split(b'\t', 1)
            candidates.append((meta.split()[1].decode(), path.decode()))
    found = []
    for oid, path in candidates:
        data = (root/path).read_bytes() if mode == 'worktree' else git('cat-file', 'blob', oid)
        rules = inspect(path, data)
        if rules: found.append({'path': path, 'object': oid, 'rules': rules})
    return {'mode': mode, 'files_scanned': len(candidates), 'findings': found}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['staged', 'worktree', 'history'], default='staged')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = scan(args.root, args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result['findings'] else 0)
