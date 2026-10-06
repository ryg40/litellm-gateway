#!/bin/sh
# Check the publish tree, messages and identity. See docs/secret-handling.md.
# Python 3 reads Git blobs without checkout filters and hashes accepted lines.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
command -v python3 >/dev/null 2>&1 || { echo 'public-check: python3 is required' >&2; exit 2; }
exec python3 - "$root" "$@" <<'PY'
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(sys.argv.pop(1))
p = argparse.ArgumentParser(description='Check a public tree without printing matched values.')
p.add_argument('--level', choices=('warn', 'fail'), default='fail')
p.add_argument('--ref', help='Check this commit instead of the index and working tree.')
p.add_argument('--identity-name', default='litellm-gateway portable')
p.add_argument('--identity-email', default='portable@litellm-gateway.invalid')
p.add_argument('--accept-identity', action='store_true')
p.add_argument('--message-file', type=Path)
p.add_argument('--commit-message-file', type=Path)
p.add_argument('mode', choices=('tree', 'selftest'), nargs='?', default='tree')
a = p.parse_args()


def git(*args, cwd=None):
    return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.DEVNULL)


def identity(name, email):
    valid = bool(re.fullmatch(r'[^<>\r\n\x00-\x1f]+', name))
    valid = valid and bool(re.fullmatch(r'[^\s<>@]+@[^\s<>@]+', email))
    neutral = bool(re.fullmatch(r'[^@]+@[^@]+\.(invalid|example)', email, re.I))
    return valid and (neutral or a.accept_identity)


def load_rules():
    rules = []
    for line in (root / 'scripts/public-patterns.tsv').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        kind, scope, pattern = line.split('\t')
        if scope not in ('docs', 'all'):
            raise ValueError('bad rule scope')
        rules.append((kind, scope, re.compile(pattern, re.I)))
    if not rules:
        raise ValueError('no public patterns')
    allowed = set()
    for line in (root / 'scripts/public-allow.tsv').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        digest, reason = line.split('\t', 1)
        if not re.fullmatch('[0-9a-f]{64}', digest) or not reason.strip():
            raise ValueError('bad accepted line')
        allowed.add(digest)
    return rules, allowed


def decode_text(data):
    # Keep original bytes for the scanner, and scan decoded UTF-16 too.
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        raise UnicodeError('unsupported text encoding')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16', 'strict')
    elif len(data) >= 4 and b'\x00' in data:
        even, odd = data[::2], data[1::2]
        if odd.count(0) / len(odd) >= 0.5 and even.count(0) / len(even) < 0.2:
            text = data.decode('utf-16-le', 'strict')
        elif even.count(0) / len(even) >= 0.5 and odd.count(0) / len(odd) < 0.2:
            text = data.decode('utf-16-be', 'strict')
        else:
            text = data.decode('utf-8', 'strict')
    else:
        text = data.decode('utf-8', 'strict')
    return text


def literal_hits(text, values):
    return any(value in text.casefold() for value in values)


def load_deny():
    values = []
    for path in ('scripts/host-values.deny', '.local/host-values.deny'):
        file = root / path
        if file.exists():
            values.extend(line.strip().casefold() for line in file.read_text().splitlines()
                          if line.strip() and not line.lstrip().startswith('#'))
    return values


def wording(path, data, metadata=False):
    hits = set()
    text = decode_text(data)
    docs = Path(path).suffix.lower() in ('.md', '.rst', '.txt', '.markdown', '.mdx', '.adoc')
    docs = docs or not Path(path).suffix
    for number, line in enumerate(text.splitlines(), 1):
        if literal_hits(line, deny):
            hits.add(('private-literal', path, number))
        key = hashlib.sha256(path.encode('utf-8') + b'\0' + line.encode('utf-8')).hexdigest()
        if not metadata and key in allowed:
            continue
        for kind, scope, regex in rules:
            if scope == 'docs' and (metadata or not docs):
                continue
            if metadata and path == '(identity)' and kind == 'email' and a.accept_identity:
                continue
            if regex.search(line):
                hits.add((kind, path, number))
    return hits


def forbidden(path):
    parts = path.lower().split('/')
    name = parts[-1]
    return (any(x in ('.local', '.env', 'secrets', 'state', 'data', '.credentials', '.ssh') for x in parts)
            or (name.startswith('.env.') and name != '.env.example')
            or any(x in ('.aws', '.ssh') for x in parts)
            or name in ('.netrc', '.git-credentials', 'credentials.json', '.envrc', '.npmrc',
                        '.pypirc', '.htpasswd', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
                        'id_rsa.pub', 'id_dsa.pub', 'id_ecdsa.pub', 'id_ed25519.pub')
            or '/'.join(parts[-2:]) == '.docker/config.json'
            or name.endswith(('.key', '.pem', '.p12', '.pfx', '.keystore', '.kdbx')))


try:
    rules, allowed = load_rules()
    deny = load_deny()
    if a.mode == 'selftest':
        samples = {'date': '20' + '26-01-02', 'tracker': 'iss' + 'ue 42',
                   'process': 'co' + 'ordinator', 'machine': '/ro' + 'ot/private',
                   'email': 'person@' + 'gmail.com'}
        for kind, sample in samples.items():
            if not any(hit[0] == kind for hit in wording('sample.md', sample.encode())):
                raise ValueError('negative control failed: ' + kind)
        if wording('clean.md', b'Use /home/you and a neutral example.'):
            raise ValueError('clean control failed')
        if identity('Example', 'person@example.com') or not identity('Portable', 'portable@project.invalid'):
            raise ValueError('identity control failed')
        synthetic = 'synthetic-' + 'deny-control'
        prior_deny = deny
        deny = [synthetic]
        if not any(hit[0] == 'private-literal' for hit in wording('control.txt', synthetic.upper().encode())):
            raise ValueError('deny-list negative control failed')
        if wording('control.txt', b'clean'):
            raise ValueError('deny-list clean control failed')
        deny = prior_deny
        scan = subprocess.run(['sh', str(root / 'scripts/scan.sh'), 'selftest'],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if scan.returncode:
            print('public-check: scanner selftest failed; values are withheld')
            sys.exit(1 if scan.returncode == 1 else 2)
        print('public-check selftest: classes, clean text, identities, deny list and scanner passed')
        sys.exit(0)

    top = Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip())
    os.chdir(top)
    hits = set()
    policy = set()
    messages = []
    identities = [(a.identity_name, a.identity_email)]
    if a.ref:
        ref = git('rev-parse', '--verify', a.ref + '^{commit}').decode().strip()
        entries = git('ls-tree', '-rz', ref).split(b'\0')
        records = []
        for entry in entries:
            if not entry:
                continue
            meta, path = entry.split(b'\t', 1)
            mode, kind, oid = meta.split()
            records.append((mode, oid, path))
        fields = git('show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', ref).split(b'\0', 4)
        identities.extend((fields[i].decode(), fields[i + 1].decode()) for i in (0, 2))
        messages.append(('commit-message', fields[4]))
    else:
        records = []
        for entry in git('ls-files', '-sz').split(b'\0'):
            if not entry:
                continue
            meta, path = entry.split(b'\t', 1)
            mode, oid, stage = meta.split()
            if stage != b'0':
                raise ValueError('unmerged index')
            records.append((mode, oid, path))
    for option, label in ((a.message_file, 'tag-message'), (a.commit_message_file, 'commit-message')):
        if option is not None:
            data = option.read_bytes()
            if not data.strip() or b'\0' in data:
                raise ValueError('message is empty or contains a NUL byte')
            messages.append((label, data))
    for name, email in identities:
        if not identity(name, email):
            policy.add(('identity', '(identity)'))
        hits.update(wording('(identity)', (name + ' <' + email + '>').encode(), True))

    # A private temporary repository lets the existing scanner read exact blobs
    # and message text with the same host lists. Its output remains redacted.
    with tempfile.TemporaryDirectory(prefix='public-check-') as tmp:
        work = Path(tmp)
        git('init', '-q', str(work))
        payload = work
        decoded = Path(tempfile.mkdtemp(prefix='public-decoded-', dir=work))

        def scan_encoding(data):
            text = decode_text(data)
            if text.encode() != data:
                (decoded / str(len(list(decoded.iterdir())))).write_text(text, encoding='utf-8')

        for mode, oid, rawpath in records:
            path = os.fsdecode(rawpath)
            if any(ord(c) < 32 for c in path) or '\\' in path:
                raise ValueError('unsupported file name')
            if forbidden(path) or mode == b'160000':
                policy.add(('path', path))
            if mode == b'160000':
                continue
            data = git('cat-file', 'blob', oid.decode())
            hits.update(wording(path, data))
            scan_encoding(data)
            hits.update(wording('(file name) ' + path, rawpath, True))
            dest = payload / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if not a.ref:
                src = top / path
                if src.is_symlink():
                    current = os.fsencode(os.readlink(src))
                elif src.exists():
                    current = src.read_bytes()
                else:
                    continue
                hits.update(wording(path, current))
                scan_encoding(current)
        metadata = Path(tempfile.mkdtemp(prefix='public-metadata-', dir=work))
        for label, data in messages:
            hits.update(wording('(' + label + ')', data, True))
            (metadata / label).write_bytes(data)
        # Metadata goes through secret and local-value rules as well as wording.
        (metadata / 'identities').write_text('\n'.join(n + ' <' + e + '>' for n, e in identities))
        git('add', '-f', '.', cwd=work)
        env = os.environ.copy()
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
            env.pop(key, None)
        scan = subprocess.run(['sh', str(root / 'scripts/scan.sh'), '--level', 'fail', 'tree'],
                              cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if scan.returncode:
            print('public-check: secret/host scan failed; matched values are withheld')
            sys.exit(1 if scan.returncode == 1 else 2)
    if not a.ref:
        scan = subprocess.run(['sh', str(root / 'scripts/scan.sh'), '--level', 'fail', 'tree'],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if scan.returncode:
            print('public-check: working-tree secret/host scan failed; matched values are withheld')
            sys.exit(1 if scan.returncode == 1 else 2)
    def safe_path(path):
        return '(path withheld)' if literal_hits(path, deny) else path

    for kind, path in sorted(policy):
        print('FAIL policy ' + kind + ' ' + safe_path(path))
    for kind, path, number in sorted(hits):
        level = 'FAIL' if kind == 'private-literal' else a.level.upper()
        print(f'{level} {kind} {safe_path(path)}:{number}')
    print(f'public-check: {len(hits)} wording finding(s), {len(policy)} policy finding(s), level {a.level}')
    private = any(kind == 'private-literal' for kind, _, _ in hits)
    sys.exit(1 if policy or private or (hits and a.level == 'fail') else 0)
except UnicodeError:
    print('public-check: unsupported or malformed text encoding', file=sys.stderr)
    sys.exit(2)
except (OSError, ValueError, subprocess.SubprocessError) as exc:
    # Errors can include source text or a private remote URL. Do not echo them.
    print('public-check: tool or input error (' + type(exc).__name__ + ')', file=sys.stderr)
    sys.exit(2)
PY
