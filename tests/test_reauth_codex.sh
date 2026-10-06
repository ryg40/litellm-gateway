#!/bin/sh
# Offline test for scripts/reauth-codex.sh. It uses a temporary copy of the tree
# and a fake docker command on PATH, so no container starts and no network is used.
#
# Usage: sh tests/test_reauth_codex.sh

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

work=$(mktemp -d "${TMPDIR:-/tmp}/litellm-reauth-test.XXXXXX")
trap 'rm -rf "$work"' EXIT
# In sh, an INT or TERM handler does not end the script; exit runs the EXIT trap.
trap 'exit 130' INT TERM

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
expect() {
    name=$1; shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}
not() { ! "$@"; }
expect_status() {
    name=$1; want=$2; shift 2
    got=0; "$@" >"$work/out" 2>&1 </dev/null || got=$?
    cat "$work/out" >>"$work/all-output"
    if [ "$got" -eq "$want" ]; then pass "$name"; else fail "$name (exit $got, expected $want)"; cat "$work/out"; fi
}
has() { grep -q -F -- "$1" "$work/out"; }
called() { grep -q -F -- "$1" "$work/docker-calls"; }
last_call() { tail -n 1 "$work/docker-calls"; }
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
backups() { ls -1 "$tree/state/$1" | grep -c '^auth\.json\.before-reauth-' || true; }
# The stop, run and up calls in order, for example "stop:codex1 run:codex1 up:codex1 ".
sequence() {
    awk '{ for (i = 1; i < NF; i++) if ($i == "stop" || $i == "run" || $i == "up") { printf "%s:%s ", $i, ($i == "run" ? $(NF - 1) : $NF); break } }' "$work/docker-calls"
}

# The token values are sentinels. No output may contain them.
old_token=OLD-SENTINEL-NOT-FOR-OUTPUT
new_token=NEW-SENTINEL-NOT-FOR-OUTPUT
write_token() { # <file> <access token> <account id>
    printf '{"access_token": "%s", "refresh_token": "%s", "account_id": "%s", "expires_at": 4102444800}\n' \
        "$2" "$2" "$3" >"$1"
    chmod 600 "$1"
}

# The fake docker command. It records each call. The script calls it in the tree.
# The files in the directory fake of the tree set its answers:
#   login            ok, fail, empty (no account_id), none (no file), mode (mode 0644),
#                    owner (another owner), interrupt, hup, term (a signal to the script)
#   login-<service>  the same for one service
#   health-<service> the health of the service (default healthy)
#   status-<service> the status of the service (default running); "fail": inspect fails
#   stop-fail        the stop fails
#   up-fail          the start fails
#   inspect-int      one inspect after a start sends INT to the script
#   oneoff           the ids of the one-off containers; a login adds the id oneoff-new
#   ps-fail          docker ps fails
#   router-log       the log of codex-router
#   logs-fail        the log is not available
bin=$work/bin
mkdir -p "$bin"
cat >"$bin/docker" <<EOF
#!/bin/sh
work=$work
new_token=$new_token
EOF
cat >>"$bin/docker" <<'EOF'
echo "$*" >>"$work/docker-calls"
fake=$PWD/fake
setting() { if [ -f "$fake/$1" ]; then cat "$fake/$1"; else echo "$2"; fi; }
new_file() { printf '{"access_token": "%s", "account_id": "%s", "expires_at": 4102444800}\n' "$new_token" "$new_token" >"$1"; }
case "$1" in
    inspect)
        for last in "$@"; do :; done
        service=${last#cid-}
        status=$(setting "status-$service" running)
        [ "$status" != fail ] || exit 1
        if [ -f "$fake/inspect-int" ] && [ -f "$fake/up-done" ]; then
            rm -f "$fake/inspect-int"
            kill -INT "$(cat "$work/pid")"
        fi
        echo "$status $(setting "health-$service" healthy)"
        exit 0 ;;
    ps)
        [ ! -f "$fake/ps-fail" ] || exit 1
        setting oneoff ""; exit 0 ;;
    rm) exit 0 ;;
    compose) ;;
    *) exit 97 ;;
esac
shift
while [ "$1" = -f ]; do shift 2; done
command=$1
for service in "$@"; do :; done
case "$command" in
    stop)
        [ ! -f "$fake/stop-fail" ] || exit 1
        echo exited >"$fake/status-$service" ;;
    up)
        [ ! -f "$fake/up-fail" ] || exit 1
        : >"$fake/up-done"
        echo running >"$fake/status-$service" ;;
    ps) echo "cid-$service" ;;
    logs)
        [ ! -f "$fake/logs-fail" ] || exit 1
        setting router-log "" ;;
    run)
        service=$(printf '%s\n' "$@" | grep '^codex[123]$')
        file=state/$service/auth.json
        [ "$(setting "status-$service" running)" = exited ] || echo "$service" >>"$work/login-while-running"
        umask 077
        # The container of this login. An id in the file before the login is another container.
        echo oneoff-new >>"$fake/oneoff"
        case "$(setting "login-$service" "$(setting login ok)")" in
            ok) new_file "$file" ;;
            empty) echo '{"access_token": "", "device_code_requested_at": 1}' >"$file" ;;
            mode) umask 022; new_file "$file" ;;
            owner) new_file "$file"; chown 4242:4242 "$file" ;;
            none) ;;
            fail) echo '{"device_code_requested_at": 1}' >"$file"; exit 1 ;;
            interrupt) echo '{"device_code_requested_at": 1}' >"$file"; kill -INT "$(cat "$work/pid")"; exit 130 ;;
            # The login does not end by itself: the script must not wait for it.
            hup) echo '{"device_code_requested_at": 1}' >"$file"; kill -HUP "$(cat "$work/pid")"; exec sleep 30 ;;
            term) echo '{"device_code_requested_at": 1}' >"$file"; kill -TERM "$(cat "$work/pid")"; exec sleep 30 ;;
        esac ;;
    *) exit 97 ;;
esac
EOF
# A fake mv. The files in the directory fake of the tree set its answers:
#   mv-fail        a "mv -f" (the restore) fails
#   mv-first-fail  the move of the old token file fails
#   mv-int         the move of the old token file sends INT to the script, then moves
#   mv-int-early   the move of the old token file sends INT to the script and does not move
real_mv=$(command -v mv)
cat >"$bin/mv" <<EOF
#!/bin/sh
if [ "\$1" = -f ]; then
    [ ! -f fake/mv-fail ] || exit 1
else
    [ ! -f fake/mv-first-fail ] || exit 1
    if [ -f fake/mv-int-early ]; then kill -INT "\$(cat "$work/pid")"; exit 1; fi
    [ ! -f fake/mv-int ] || kill -INT "\$(cat "$work/pid")"
fi
exec "$real_mv" "\$@"
EOF
chmod +x "$bin/docker" "$bin/mv"
PATH=$bin:$PATH
REAUTH_HEALTH_TIMEOUT=2 REAUTH_POLL_SECONDS=1
export PATH REAUTH_HEALTH_TIMEOUT REAUTH_POLL_SECONDS
unset DOCKER 2>/dev/null || true

# A new copy of the tree with the three accounts and no fake setting.
trees=0
new_tree() {
    trees=$((trees + 1))
    tree=$work/tree$trees
    fake=$tree/fake
    reauth=$tree/scripts/reauth-codex.sh
    mkdir -p "$tree/scripts" "$fake"
    cp "$repo_root/scripts/reauth-codex.sh" "$repo_root/scripts/login-codex.sh" \
        "$repo_root/scripts/login-codex.py" "$tree/scripts/"
    cp "$repo_root/compose.yaml" "$repo_root/compose.codex.yaml" "$tree/"
    for n in 1 2 3; do
        mkdir -p "$tree/state/codex$n"
        write_token "$tree/state/codex$n/auth.json" "$old_token" "$old_token"
    done
    : >"$work/docker-calls"
    : >"$work/login-while-running"
}
# Run the script with its process id in $work/pid, so that the fake docker can send a signal.
with_pid() { sh -c 'echo $$ >"$1/pid"; shift; exec sh "$@"' sh "$work" "$reauth" "$@"; }
is_old() { grep -q -F "$old_token" "$tree/state/$1/auth.json"; }
is_new() { grep -q -F "$new_token" "$tree/state/$1/auth.json"; }
up_line="compose -f compose.yaml -f compose.codex.yaml up -d"

# Usage errors call no docker command.
new_tree
expect_status "no argument" 2 sh "$reauth"
expect_status "unknown account" 2 sh "$reauth" 4
expect_status "unknown option" 2 sh "$reauth" 1 --force
expect_status "two accounts" 2 sh "$reauth" 1 2
REAUTH_POLL_SECONDS=0 expect_status "poll time 0" 2 sh "$reauth" 1 --yes
expect "message names the variable" has "REAUTH_POLL_SECONDS must be a positive integer"
REAUTH_POLL_SECONDS=1.5 expect_status "poll time that is not an integer" 2 sh "$reauth" 1 --yes
REAUTH_HEALTH_TIMEOUT=soon expect_status "timeout that is not a number" 2 sh "$reauth" 1 --yes
expect "usage errors do not call docker" [ ! -s "$work/docker-calls" ]
"$real_mv" "$tree/state/codex2" "$tree/state/codex2.away"
expect_status "missing state directory" 2 sh "$reauth" 2 --yes
expect "message names the directory" has "state/codex2 does not exist"
expect_status "all with a missing state directory" 2 sh "$reauth" all --yes
expect "a missing directory stops the script before docker" [ ! -s "$work/docker-calls" ]
if [ "$(id -u)" != 0 ]; then
    chmod 000 "$tree/state/codex3"
    expect_status "not root: unreadable state directory" 2 sh "$reauth" 3 --check
    expect "not root: message names the user" has "cannot use the directory"
    chmod 700 "$tree/state/codex3"
fi

# Without --yes the script asks. No answer changes nothing.
new_tree
expect_status "no confirmation" 1 sh "$reauth" 1
expect "the plan is shown" has "Plan for codex1:"
expect "the plan names the time without the service" has "The service codex1 is away from step 1"
expect "no stop without confirmation" not called "stop codex1"
expect "the token file stays" is_old codex1

# The happy path.
new_tree
expect_status "happy path" 0 sh "$reauth" 3 --yes
expect "result line" has "codex3: OK: new login, service healthy"
expect "router result says what was seen" has "no skip line in the router log since the login. Not a proof: The router logs a skip only on a request"
expect "stop of the service" called "compose -f compose.yaml -f compose.codex.yaml stop codex3"
expect "login in a container of the service" called "run --rm --no-deps"
expect "the service is stopped during the login" [ ! -s "$work/login-while-running" ]
expect "start of the service" called "$up_line codex3"
expect "the router log is read" called "--no-log-prefix codex-router"
expect "the other services are not stopped" not called "stop codex1"
expect "the new token file is in place" is_new codex3
expect "the new token file has mode 0600" [ "$(mode_of "$tree/state/codex3/auth.json")" = 600 ]
expect "one backup" [ "$(backups codex3)" = 1 ]
backup=$(ls -1 "$tree/state/codex3" | grep '^auth\.json\.before-reauth-[0-9]\{8\}T[0-9]\{6\}Z$' || true)
expect "the backup name has the UTC time" [ -n "$backup" ]
expect "the backup has mode 0600" [ "$(mode_of "$tree/state/codex3/$backup")" = 600 ]
expect "the backup holds the old file" grep -q -F "$old_token" "$tree/state/codex3/$backup"
expect "order: stop, login, start" [ "$(sequence)" = "stop:codex3 run:codex3 up:codex3 " ]

# A first login: no old file.
new_tree
unlink "$tree/state/codex3/auth.json"
echo exited >"$fake/status-codex3"
expect_status "login with no old file" 0 sh "$reauth" 3 --yes
expect "no backup" [ "$(backups codex3)" = 0 ]
expect "the new token file is in place" is_new codex3
expect "the service starts after a first login" called "up -d codex3"

# A router skip after the login gives a warning and keeps the new file.
new_tree
echo "INFO codex2 skipped source=no-login" >"$fake/router-log"
expect_status "router skip after the login" 1 sh "$reauth" 2 --yes
expect "warning line" has "codex2: WARNING"
expect "the new token file stays" is_new codex2
expect_status "a skip of another account is not a warning" 0 sh "$reauth" 3 --yes
# No router log: the result says so.
new_tree
: >"$fake/logs-fail"
expect_status "router log not available" 0 sh "$reauth" 1 --yes
expect "the result names the missing log" has "the router log was not available. Not a proof:"

# A login failure restores the backup and starts the service again.
for kind in fail none empty mode owner; do
    if [ "$kind" = owner ] && [ "$(id -u)" != 0 ]; then
        echo "skip - owner: only root can give the file to another owner"
        continue
    fi
    new_tree
    echo "$kind" >"$fake/login"
    expect_status "login result $kind" 1 sh "$reauth" 1 --yes
    expect "$kind: failure line" has "codex1: FAILED:"
    expect "$kind: the old token file is back" is_old codex1
    expect "$kind: the old token file has mode 0600" [ "$(mode_of "$tree/state/codex1/auth.json")" = 600 ]
    expect "$kind: no backup stays" [ "$(backups codex1)" = 0 ]
    expect "$kind: the service is started again" [ "$(last_call)" = "$up_line codex1" ]
    expect "$kind: message about the start" has "the service is started again"
done
expect "owner: the reason names the owner" has "another owner"

# A failed stop changes no file and starts the service.
new_tree
: >"$fake/stop-fail"
expect_status "failed stop" 1 sh "$reauth" 1 --yes
expect "failed stop: reason" has "codex1: FAILED: the service did not stop"
expect "failed stop: message about the file" has "the token file is not changed"
expect "failed stop: the token file stays" is_old codex1
expect "failed stop: no backup" [ "$(backups codex1)" = 0 ]
expect "failed stop: no login" not called "run --rm"
expect "failed stop: the service is started" [ "$(last_call)" = "$up_line codex1" ]

# After the new file passed the checks, it always stays.
new_tree
: >"$fake/up-fail"
expect_status "failed start after a complete login" 1 sh "$reauth" 1 --yes
expect "failed start: reason" has "codex1: FAILED: the service did not start"
expect "failed start: the new token file stays" is_new codex1
expect "failed start: the backup stays" [ "$(backups codex1)" = 1 ]
expect "failed start: message about the new file" has "the new login is complete and stays at state/codex1/auth.json"
expect "failed start: message about the backup" has "the old token file is still at state/codex1/auth.json.before-reauth-"
expect "failed start: manual command" has "&& docker compose -f compose.yaml -f compose.codex.yaml up -d codex1"
expect "failed start: a second start was tried" [ "$(grep -c -F "$up_line codex1" "$work/docker-calls")" = 2 ]

new_tree
echo starting >"$fake/health-codex1"
expect_status "service not healthy" 1 sh "$reauth" 1 --yes
expect "not healthy: the reason names the health" has "not healthy after 2 s"
expect "not healthy: the new token file stays" is_new codex1
expect "not healthy: the backup stays" [ "$(backups codex1)" = 1 ]
expect "not healthy: message about the backup" has "the old token file is still at"

new_tree
: >"$fake/inspect-int"
echo starting >"$fake/health-codex2"
expect_status "interrupt during the wait for the health" 130 with_pid 2 --yes
expect "wait interrupt: failure line" has "codex2: FAILED: interrupted (INT)"
expect "wait interrupt: the new token file stays" is_new codex2
expect "wait interrupt: the backup stays" [ "$(backups codex2)" = 1 ]
expect "wait interrupt: the service start is sent" [ "$(last_call)" = "$up_line codex2" ]

# The state of the service before the run decides the start after a failure.
new_tree
echo fail >"$fake/login"
echo exited >"$fake/status-codex3"
expect_status "failure for a stopped service" 1 sh "$reauth" 3 --yes
expect "the old token file is back" is_old codex3
expect "a stopped service stays stopped" not called "up -d codex3"
expect "message about the stopped service" has "did not run before, so it stays stopped"
for state in restarting fail; do
    new_tree
    echo fail >"$fake/login"
    echo "$state" >"$fake/status-codex3"
    expect_status "failure for a service with the state $state" 1 sh "$reauth" 3 --yes
    expect "$state: the old token file is back" is_old codex3
    expect "$state: the service is started" [ "$(last_call)" = "$up_line codex3" ]
done

# A failure with no old file removes the file of the failed login.
new_tree
unlink "$tree/state/codex3/auth.json"
echo fail >"$fake/login"
expect_status "failure with no old file" 1 sh "$reauth" 3 --yes
expect "no token file stays" [ ! -e "$tree/state/codex3/auth.json" ]

# A failed restore: the service stays stopped and the script prints the command.
new_tree
echo fail >"$fake/login"
: >"$fake/mv-fail"
expect_status "failed restore" 1 sh "$reauth" 1 --yes
expect "failed restore: message" has "the old token file is NOT back. The service stays stopped."
backup=$(ls -1 "$tree/state/codex1" | grep '^auth\.json\.before-reauth-' || true)
expect "failed restore: the backup stays" grep -q -F "$old_token" "$tree/state/codex1/$backup"
expect "failed restore: manual command" has "cd $tree && mv state/codex1/$backup state/codex1/auth.json && docker compose -f compose.yaml -f compose.codex.yaml up -d codex1"
expect "failed restore: the service is not started" not called "up -d codex1"

# A signal during the login restores. The fake login sends the signal to the script.
# For HUP and TERM the fake login does not end; the script must not wait for it.
for case in interrupt:INT:130 hup:HUP:129 term:TERM:143; do
    kind=${case%%:*}; signal=${case#*:}; signal=${signal%:*}; status=${case##*:}
    new_tree
    echo "$kind" >"$fake/login"
    echo oneoff-old >"$fake/oneoff"
    begin=$(date +%s)
    expect_status "$signal during the login" "$status" with_pid 2 --yes
    expect "$signal: the script does not wait for the login" [ $(($(date +%s) - begin)) -lt 15 ]
    expect "$signal: failure line" has "codex2: FAILED: interrupted ($signal)"
    expect "$signal: the old token file is back" is_old codex2
    expect "$signal: no backup stays" [ "$(backups codex2)" = 0 ]
    expect "$signal: the login container is removed" called "rm -f oneoff-new"
    expect "$signal: a container from before the login stays" not called "oneoff-old"
    expect "$signal: the service is started again" [ "$(last_call)" = "$up_line codex2" ]
done

# docker ps fails: the script removes no container and says so in one line.
new_tree
echo term >"$fake/login"
: >"$fake/ps-fail"
expect_status "TERM during the login with a failed docker ps" 143 with_pid 2 --yes
expect "ps fails: one message line" [ "$(grep -c -F "docker ps failed" "$work/out")" = 1 ]
expect "ps fails: no container is removed" not called "rm -f"
expect "ps fails: the old token file is back" is_old codex2

# A signal during the move of the old token file restores it.
new_tree
: >"$fake/mv-int"
expect_status "INT during the move" 130 with_pid 2 --yes
expect "move signal: failure line" has "codex2: FAILED: interrupted (INT)"
expect "move signal: the old token file is back" is_old codex2
expect "move signal: message about the restore" has "the old token file is back at state/codex2/auth.json"
expect "move signal: no backup stays" [ "$(backups codex2)" = 0 ]
expect "move signal: no login" not called "run --rm"
expect "move signal: the service is started again" [ "$(last_call)" = "$up_line codex2" ]
# A signal before the move, and a move that fails: the file was never moved.
for kind in mv-int-early mv-first-fail; do
    new_tree
    : >"$fake/$kind"
    got=0; with_pid 2 --yes >"$work/out" 2>&1 </dev/null || got=$?
    cat "$work/out" >>"$work/all-output"
    expect "$kind: the exit status is not 0" [ "$got" -ne 0 ]
    expect "$kind: message about the file" has "codex2: the token file is not changed."
    expect "$kind: no message about a missing file" not has "NOT back"
    expect "$kind: the old token file is in place" is_old codex2
    expect "$kind: no backup" [ "$(backups codex2)" = 0 ]
    expect "$kind: no login" not called "run --rm"
    expect "$kind: the service is started again" [ "$(last_call)" = "$up_line codex2" ]
done

# all: one account at a time, in order, and stop at the first failure.
new_tree
expect_status "all" 0 sh "$reauth" all --yes
expect "all: three result lines" [ "$(grep -c ': OK: ' "$work/out")" = 3 ]
expect "all: order of the calls" [ "$(sequence)" = "stop:codex1 run:codex1 up:codex1 stop:codex2 run:codex2 up:codex2 stop:codex3 run:codex3 up:codex3 " ]
expect "all: each login runs with only its service stopped" [ ! -s "$work/login-while-running" ]
new_tree
echo fail >"$fake/login-codex2"
expect_status "all with a failure of codex2" 1 sh "$reauth" all --yes
expect "all: codex1 is done" is_new codex1
expect "all: codex2 is restored" is_old codex2
expect "all: codex2 is started again" called "up -d codex2"
expect "all: codex3 is not touched" not called "stop codex3"
expect "all: codex3 keeps its file" is_old codex3
expect "all: no plan for codex3" not has "Plan for codex3"

# At most 3 backups stay after a good run. Only the exact names of the script count.
old_backups() {
    for stamp in 20200101T000000Z 20210101T000000Z 20220101T000000Z 20230101T000000Z 20991231T235959Z; do
        echo '{}' >"$tree/state/codex1/auth.json.before-reauth-$stamp"
    done
    # Names that the script does not write.
    echo '{}' >"$tree/state/codex1/auth.json.before-reauth-20260101"
    echo '{}' >"$tree/state/codex1/auth.json.before-reauth-20200101T000000Z.keep"
}
new_tree
old_backups
echo fail >"$fake/login"
expect_status "failed run with old backups" 1 sh "$reauth" 1 --yes
expect "a failed run removes no backup" [ "$(backups codex1)" = 7 ]
expect "a failed run restores the old file" is_old codex1
new_tree
old_backups
expect_status "good run with old backups" 0 sh "$reauth" 1 --yes
expect "three backups and the two other names stay" [ "$(backups codex1)" = 5 ]
expect "the backup of this run stays" [ "$(grep -l -F "$old_token" "$tree"/state/codex1/auth.json.before-reauth-* | wc -l)" -eq 1 ]
expect "the two newest other backups stay" [ -e "$tree/state/codex1/auth.json.before-reauth-20991231T235959Z" ]
expect "the second newest stays" [ -e "$tree/state/codex1/auth.json.before-reauth-20230101T000000Z" ]
expect "the older backups are removed" [ ! -e "$tree/state/codex1/auth.json.before-reauth-20220101T000000Z" ]
expect "the oldest backup is removed" [ ! -e "$tree/state/codex1/auth.json.before-reauth-20200101T000000Z" ]
expect "the hand-made name stays" [ -e "$tree/state/codex1/auth.json.before-reauth-20260101" ]
expect "a name with a suffix stays" [ -e "$tree/state/codex1/auth.json.before-reauth-20200101T000000Z.keep" ]

# --check changes nothing and reports each account.
new_tree
echo '{"access_token": "", "device_code_requested_at": 1}' >"$tree/state/codex2/auth.json"
chmod 644 "$tree/state/codex2/auth.json"
unlink "$tree/state/codex3/auth.json"
echo exited >"$fake/status-codex3"
echo none >"$fake/health-codex3"
echo starting >"$fake/health-codex2"
echo "INFO codex2 skipped source=no-login" >"$fake/router-log"
before=$(ls -lR "$tree/state")
expect_status "check of one complete account" 0 sh "$reauth" 1 --check
expect "check: one line and the note" [ "$(wc -l <"$work/out")" -eq 2 ]
expect "check: complete account" has "codex1: service=running health=healthy file=yes mode=600 login=complete expires_in="
expect "check: hours to expires_at" grep -q -E 'expires_in=[0-9]+\.[0-9]h router_skip_line_2m=none$' "$work/out"
expect "check: the note says that no skip line is not a proof" has 'Note: The router logs a skip only on a request, and never for the last account of its order. "none" is not a proof'
expect_status "check of all accounts" 1 sh "$reauth" all --check
expect "check: three lines and the note" [ "$(wc -l <"$work/out")" -eq 4 ]
expect "check: empty file" has "codex2: service=running health=starting file=yes mode=644 login=incomplete expires_in=- router_skip_line_2m=yes"
expect "check: missing file" has "codex3: service=exited health=none file=no mode=- login=missing expires_in=- router_skip_line_2m=none"
expect_status "check with --yes" 1 sh "$reauth" --check 2 --yes
echo 'not json' >"$tree/state/codex1/auth.json"
expect_status "check of an invalid file" 1 sh "$reauth" 1 --check
expect "check: invalid file" has "login=invalid"
write_token "$tree/state/codex1/auth.json" "$old_token" "$old_token"
expect "check reads the last 2 minutes of the router log" called "logs --since 2m --no-log-prefix codex-router"
expect "check stops, starts and runs nothing" [ -z "$(sequence)" ]
expect "check changes no file" [ "$before" = "$(ls -lR "$tree/state")" ]

# DOCKER names the docker command of the script.
new_tree
cp "$bin/docker" "$work/other-docker"
PATH=${PATH#"$bin":} DOCKER=$work/other-docker expect_status "DOCKER is used" 0 sh "$reauth" 1 --check
expect "DOCKER: the command was called" called "ps -a -q codex1"

# No token value in any output or in the docker calls.
expect "the outputs exist" [ -s "$work/all-output" ]
expect "no old token value in an output" not grep -q -F "$old_token" "$work/all-output"
expect "no new token value in an output" not grep -q -F "$new_token" "$work/all-output"
expect "no token value in the docker calls" not grep -q -F -e "$old_token" -e "$new_token" "$work/docker-calls"

# The script and the services use the same paths.
for n in 1 2 3; do
    expect "codex$n mounts state/codex$n at /tokens" grep -q -F -- "- ./state/codex$n:/tokens" "$repo_root/compose.codex.yaml"
done
expect "the login script runs a container of the service" grep -q -F 'run --rm --no-deps' "$repo_root/scripts/login-codex.sh"
expect "the script handles HUP" grep -q "^trap .* HUP$" "$repo_root/scripts/reauth-codex.sh"

if [ "$failures" -gt 0 ]; then
    echo "$failures failure(s)"
    exit 1
fi
echo "all tests passed"
