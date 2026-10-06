#!/bin/sh
# Exercise the public rules in a temporary Git repository with a scan double.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/public-check-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM
HOME=$tmp GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export HOME GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
mkdir -p "$tmp/r/scripts"
cp "$repo/scripts/public_check.sh" "$repo/scripts/public-patterns.tsv" "$repo/scripts/public-allow.tsv" "$tmp/r/scripts/"
cat >"$tmp/r/scripts/scan.sh" <<'SH'
#!/bin/sh
[ "$*" = '--level fail tree' ] || [ "$*" = selftest ] || exit 2
exit "${TEST_SCAN_STATUS:-0}"
SH
cd "$tmp/r"
mkdir .local
printf 'synthetic-host-only\n' >.local/host-values.deny
git init -q
# The script files stay untracked: the fixture controls the publish set.
printf 'Use /home/you for examples.\n' >sample.md
git add sample.md
count=0
expect() {
    label=$1 wanted=$2; shift 2
    status=0
    "$@" >"$tmp/next-out" 2>&1 || status=$?
    mv "$tmp/next-out" "$tmp/out"
    if [ "$status" != "$wanted" ]; then
        printf 'FAIL %s: expected %s, got %s\n' "$label" "$wanted" "$status"
        cat "$tmp/out"; exit 1
    fi
    reason=
    case $label in
        'private path fails even at warn'|'personal identity fails'|'snapshot metadata checked') reason='FAIL policy' ;;
        'changed accepted content fails') reason='FAIL tracker sample.md:' ;;
        'tag message checked') reason='FAIL tracker (tag-message):1' ;;
        'commit message checked') reason='FAIL tracker (commit-message):1' ;;
        *'fails') reason='wording finding(s)' ;;
        'scan finding fails at warn'|'scan tool error fails closed') reason='scan failed' ;;
    esac
    if [ -n "$reason" ] && ! grep -qF "$reason" "$tmp/out"; then
        echo "FAIL $label: wrong reason"; cat "$tmp/out"; exit 1
    fi
    count=$((count + 1)); printf 'ok %s\n' "$label"
}
reject() {
    label=$1 reason=$2; shift 2
    expect "$label" 1 "$@"
    grep -qF -- "$reason" "$tmp/out" || { echo "FAIL $label: wrong reason"; cat "$tmp/out"; exit 1; }
}
expect 'selftest' 0 sh scripts/public_check.sh selftest
expect 'clean tree' 0 sh scripts/public_check.sh
for kind in date tracker process machine; do
    case $kind in
        date) printf '20%s\n' 26-01-02 >sample.md ;;
        tracker) printf 'iss%s 12\n' ue >sample.md ;;
        process) printf 'co%s\n' ordinator >sample.md ;;
        machine) printf '/ro%s/private\n' ot >sample.md ;;
    esac
    git add sample.md
    expect "$kind fails" 1 sh scripts/public_check.sh
    expect "$kind reported without content" 0 grep -F "FAIL $kind sample.md:1" "$tmp/out"
    expect "$kind warns" 0 sh scripts/public_check.sh --level warn
    printf 'clean\n' >sample.md
    expect "$kind stays visible in index" 1 sh scripts/public_check.sh
    grep -qF "FAIL $kind sample.md:1" "$tmp/out"
    git add sample.md
done
printf 'iss%s 23\n' ue >sample.md
git add sample.md
python3 - <<'PY'
from pathlib import Path
import hashlib
line = Path('sample.md').read_bytes().rstrip(b'\n')
with Path('scripts/public-allow.tsv').open('a') as f:
    f.write(hashlib.sha256(b'sample.md\0' + line).hexdigest() + '\tSynthetic test example.\n')
PY
expect 'exact accepted line' 0 sh scripts/public_check.sh
cp sample.md copied.md
git add copied.md
reject 'copied allow line refused' 'FAIL tracker copied.md:1' sh scripts/public_check.sh
git rm -q --cached copied.md
rm copied.md
printf ' extra\n' >>sample.md
expect 'other clean line allowed' 0 sh scripts/public_check.sh
printf 'iss%s 24\n' ue >sample.md
expect 'changed accepted content fails' 1 sh scripts/public_check.sh
printf 'clean\n' >sample.md
git add sample.md
expect 'personal identity fails' 1 sh scripts/public_check.sh --identity-email person@example.com
expect 'neutral identity passes' 0 sh scripts/public_check.sh --identity-email release@project.example
expect 'explicit identity exception' 0 sh scripts/public_check.sh --identity-email person@example.com --accept-identity
for path in .local/note .env secrets/value state/value data/value id_rsa key.pem key.key .netrc .git-credentials credentials.json .envrc .npmrc .pypirc .htpasswd .aws/config .docker/config.json vault.kdbx id_rsa.pub nested/ID_ED25519.PUB; do
    mkdir -p "$(dirname "$path")"
    printf 'clean\n' >"$path"
    git add -f "$path"
    expect 'private path fails even at warn' 1 sh scripts/public_check.sh --level warn
    grep -qF "FAIL policy path $path" "$tmp/out"
    git rm -q --cached "$path"
    rm "$path"
done
expect 'scan finding fails at warn' 1 env TEST_SCAN_STATUS=1 sh scripts/public_check.sh --level warn
expect 'scan tool error fails closed' 2 env TEST_SCAN_STATUS=2 sh scripts/public_check.sh
printf 'iss%s 25\n' ue >"$tmp/message"
expect 'tag message checked' 1 sh scripts/public_check.sh --message-file "$tmp/message"
expect 'commit message checked' 1 sh scripts/public_check.sh --commit-message-file "$tmp/message"
# Date forms cover text of any kind; compact dates cover documents only.
for path in record.py record.yaml record.html record.md record.markdown record.mdx record.adoc CHANGELOG; do
    for separator in - / .; do
        printf 'checked 20%s%s01%s02\n' 26 "$separator" "$separator" >"$path"
        git add "$path"
        reject 'date in text refused' "FAIL date $path:1" sh scripts/public_check.sh
        git rm -q --cached "$path"; rm "$path"
    done
    printf 'October %s, 20%s\n' 5 26 >"$path"
    git add "$path"
    reject 'month-name date refused' "FAIL date $path:1" sh scripts/public_check.sh
    git rm -q --cached "$path"; rm "$path"
done
printf '20%sT00:00:00Z\n' 26-01-02 >timestamp.py
git add timestamp.py
reject 'ISO timestamp in code refused' 'FAIL date timestamp.py:1' sh scripts/public_check.sh
git rm -q --cached timestamp.py; rm timestamp.py
printf '20%s\n' 260102 >fixture.py
git add fixture.py
expect 'compact code fixture remains valid' 0 sh scripts/public_check.sh
for path in record.markdown record.mdx record.adoc CHANGELOG; do
    cp fixture.py "$path"; git add "$path"
    reject 'compact document date refused' "FAIL date $path:1" sh scripts/public_check.sh
    git rm -q --cached "$path"; rm "$path"
done
git rm -q --cached fixture.py; rm fixture.py
printf 'The owner of this file is root.\n' >sample.md
git add sample.md
expect 'Unix ownership remains valid' 0 sh scripts/public_check.sh
for text in 'approval of the ow' 'the owner appro' 'owner decis' 'development ho' 'open ques' 'target environ' 'older layout of th' 'some' 'The repo r' 'Verified by ha' 'Verified with a real acc'; do
    case $text in
        'approval of the ow') suffix=ner ;; 'the owner appro') suffix=ves ;; 'owner decis') suffix=ions ;;
        'development ho') suffix=st ;; 'open ques') suffix=tions ;; 'target environ') suffix=ment ;;
        'older layout of th') suffix=is ;; 'some') suffix=one ;; 'The repo r') suffix=an ;;
        'Verified by ha') suffix=nd ;; *) suffix=ount ;;
    esac
    printf '%s%s\n' "$text" "$suffix" >sample.md; git add sample.md
    reject 'process form refused' 'FAIL process sample.md:1' sh scripts/public_check.sh
done
for text in 'iss' 'Phase-' 'stage ' 'finding '; do
    case $text in iss) suffix='ue #42' ;; Phase-) suffix=2 ;; 'stage ') suffix=D ;; *) suffix='2:' ;; esac
    printf '%s%s\n' "$text" "$suffix" >sample.md; git add sample.md
    reject 'tracker form refused' 'FAIL tracker sample.md:1' sh scripts/public_check.sh
done
printf 'stage %s\n' A >sample.md; git add sample.md
reject 'uppercase stage label refused' 'FAIL tracker sample.md:1' sh scripts/public_check.sh
printf 'Use git to stage a commit.\n' >sample.md; git add sample.md
expect 'plain staging instruction allowed' 0 sh scripts/public_check.sh
for text in /ro /opt/sta /Us /ho; do
    case $text in /ro) suffix=ot ;; /opt/sta) suffix=cks ;; /ho) suffix=me/name ;; *) suffix=ers/name ;; esac
    printf '%s%s\n' "$text" "$suffix" >sample.md; git add sample.md
    reject 'machine boundary refused' 'FAIL machine sample.md:1' sh scripts/public_check.sh
done
printf 'user@%s.com\n' gmail >sample.md; git add sample.md
reject 'personal email wording refused' 'FAIL email sample.md:1' sh scripts/public_check.sh
printf 'Use /home/you and nobody@example.com.\n' >sample.md; git add sample.md
expect 'reserved examples remain valid' 0 sh scripts/public_check.sh
printf 'synthetic-host-only\n' >sample.md; git add sample.md
reject 'private literal always fails at warn' 'FAIL private-literal sample.md:1' sh scripts/public_check.sh --level warn
printf 'clean\n' >sample.md; git add sample.md
# Encoded content is checked even with a scanner double.
for encoding in utf-16 utf-16-le utf-16-be; do
    for text in literal wording; do
        python3 - "$encoding" "$text" <<'PY'
from pathlib import Path
import sys
s = 'synthetic-host-only' if sys.argv[2] == 'literal' else 'co' + 'ordinator'
Path('encoded.txt').write_bytes(s.encode(sys.argv[1]))
PY
        git add encoded.txt
        reject 'encoded text refused' 'encoded.txt:1' sh scripts/public_check.sh
    done
done
python3 - <<'PY'
from pathlib import Path
Path('encoded.txt').write_bytes(b'\xfe\xff' + ('co' + 'ordinator').encode('utf-16-be'))
PY
git add encoded.txt
reject 'BE BOM wording refused' 'FAIL process encoded.txt:1' sh scripts/public_check.sh
python3 - <<'PY'
from pathlib import Path
Path('encoded.txt').write_bytes(b'\xff\xfeA')
PY
git add encoded.txt
expect 'malformed UTF-16 fails closed' 2 sh scripts/public_check.sh
grep -qF 'text encoding' "$tmp/out"
git rm -q --cached encoded.txt; rm encoded.txt
python3 - <<'PY'
from pathlib import Path
Path('unsupported.txt').write_bytes(b'\xff\xfe\x00\x00' + b'A\x00\x00\x00')
PY
git add unsupported.txt
expect 'UTF-32 fails closed' 2 sh scripts/public_check.sh
grep -qF 'text encoding' "$tmp/out"
git rm -q --cached unsupported.txt; rm unsupported.txt
# Binary flags do not hide readable words or a literal deny value.
printf 'clean\000synthetic-host-only\n' >flagged.bin
printf 'flagged.bin -diff\n' >.gitattributes
git add flagged.bin .gitattributes
reject 'binary literal refused' 'private-literal flagged.bin' sh scripts/public_check.sh
git rm -q --cached flagged.bin .gitattributes; rm flagged.bin .gitattributes
ln -s "$(printf '/ro%s/synthetic' ot)" symlink
git add symlink
reject 'symlink target checked' 'FAIL machine symlink:1' sh scripts/public_check.sh
git rm -q --cached symlink; rm symlink
printf 'clean\n' >synthetic-host-only.txt; git add synthetic-host-only.txt
reject 'deny value in filename refused' 'private-literal (path withheld)' sh scripts/public_check.sh
git rm -q --cached synthetic-host-only.txt; rm synthetic-host-only.txt
printf 'clean\n' >sample.md; git add sample.md
printf 'iss%s #25\r\n' ue >"$tmp/message"
reject 'CRLF tag wording refused' 'FAIL tracker (tag-message):1' sh scripts/public_check.sh --message-file "$tmp/message"
printf 'clean\000text\n' >"$tmp/message"
expect 'NUL tag input fails closed' 2 sh scripts/public_check.sh --message-file "$tmp/message"
grep -qF 'tool or input error' "$tmp/out"
# Commit metadata is checked in ref mode, without reading unrelated ancestry.
git -c user.name=Personal -c user.email=person@example.com commit -qm example
expect 'snapshot metadata checked' 1 sh scripts/public_check.sh --ref HEAD
expect 'explicit snapshot identity exception' 0 sh scripts/public_check.sh --ref HEAD --accept-identity
printf 'co%s\n' ordinator >sample.md
expect 'ref ignores uncommitted wording' 0 sh scripts/public_check.sh --ref HEAD --accept-identity
reject 'worktree wording remains checked' 'FAIL process sample.md:1' sh scripts/public_check.sh --accept-identity
git add sample.md
git -c user.name=Portable -c user.email=portable@project.invalid commit -qm example
bad_message=$(git -c user.name=Portable -c user.email=portable@project.invalid commit-tree 'HEAD^{tree}' -m "$(printf 'iss%s #26' ue)")
reject 'ref commit message wording checked' 'FAIL tracker (commit-message):1' sh scripts/public_check.sh --ref "$bad_message"
printf 'clean\n' >sample.md
git add sample.md
reject 'ref checks committed blob not index' 'FAIL process sample.md:1' sh scripts/public_check.sh --ref HEAD
git update-index --add --cacheinfo "160000,$(git rev-parse HEAD),module"
reject 'submodule refused' 'FAIL policy path module' sh scripts/public_check.sh
git update-index --force-remove module
printf 'clean\n' >"$(printf 'bad\tname')"
git add "$(printf 'bad\tname')"
expect 'control filename fails closed' 2 sh scripts/public_check.sh
grep -qF 'tool or input error' "$tmp/out"
printf 'all %s public checks passed\n' "$count"
