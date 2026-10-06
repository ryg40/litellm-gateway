#!/bin/sh
# New device-code login for the account services codex1, codex2 and codex3.
# The script stops one service, moves its token file aside, runs
# scripts/login-codex.sh and starts the service again. A failure or a signal
# before the new token file is complete puts the old token file back.
# A complete new token file always stays. The script prints no token value.
#
# Usage: sh scripts/reauth-codex.sh <1|2|3|all> [--check] [--yes]
#   --check  change nothing; print the state of each account
#   --yes    do not ask for confirmation
#
# Environment:
#   DOCKER                  the docker command of this script (default docker);
#                           scripts/login-codex.sh uses docker from PATH
#   REAUTH_HEALTH_TIMEOUT   seconds to wait for a healthy service (default 120)
#   REAUTH_POLL_SECONDS     seconds between two health checks (default 5)
#
# Exit: 0 success, 1 failure or incomplete login, 2 usage or set-up error,
# 129, 130 or 143 after the signal HUP, INT or TERM.
set -eu
cd "$(dirname "$0")/.."

usage() {
    echo "Usage: sh scripts/reauth-codex.sh <1|2|3|all> [--check] [--yes]" >&2
    exit 2
}

target= check=0 yes=0
for arg in "$@"; do
    case "$arg" in
        1|2|3|all) [ -z "$target" ] || usage; target=$arg ;;
        --check) check=1 ;;
        --yes) yes=1 ;;
        *) usage ;;
    esac
done
[ -n "$target" ] || usage
if [ "$target" = all ]; then accounts="1 2 3"; else accounts=$target; fi

docker=${DOCKER:-docker}
timeout=${REAUTH_HEALTH_TIMEOUT:-120}
poll=${REAUTH_POLL_SECONDS:-5}
case "$timeout" in
    ''|*[!0-9]*) echo "REAUTH_HEALTH_TIMEOUT must be a number of seconds." >&2; exit 2 ;;
esac
case "$poll" in
    ''|*[!0-9]*) poll=0 ;;
esac
if [ "$poll" -lt 1 ]; then
    echo "REAUTH_POLL_SECONDS must be a positive integer." >&2
    exit 2
fi

compose() { "$docker" compose -f compose.yaml -f compose.codex.yaml "$@"; }

mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
owner_of() { stat -c %u:%g "$1" 2>/dev/null || stat -f %u:%g "$1"; }

# Print "<status> <health>" of a service: for example "running healthy",
# "exited none", "absent none" when the service has no container, or
# "unknown unknown" when docker gives no answer for the container.
service_state() {
    cid=$(compose ps -a -q "$1" 2>/dev/null | sed -n 1p) || cid=
    if [ -z "$cid" ]; then
        echo "absent none"
        return 0
    fi
    "$docker" inspect --format \
        '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$cid" 2>/dev/null || echo "unknown unknown"
}

# Print "<login> <hours>" for a token file. <login> is complete, incomplete or
# invalid. <hours> is the time to expires_at, or "-". No value of the file is printed.
token_state() {
    python3 - "$1" <<'PY'
import datetime, json, sys, time
try:
    with open(sys.argv[1]) as handle:
        token = json.load(handle)
except Exception:
    print("invalid -")
    sys.exit(0)
if not isinstance(token, dict):
    print("invalid -")
    sys.exit(0)
login = "complete" if token.get("access_token") and token.get("account_id") else "incomplete"
expires = token.get("expires_at")
hours = "-"
try:
    if isinstance(expires, str):
        when = datetime.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        expires = when.timestamp()
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        if expires > 1e11:  # milliseconds
            expires = expires / 1000
        hours = "%.1f" % ((expires - time.time()) / 3600)
except Exception:
    hours = "-"
print(login, hours)
PY
}

# Print yes, none or unknown: the log of codex-router since $2 has a skip line of
# the account. The router logs a skip only when a request arrives, and never for the
# last account of CODEX_ACCOUNT_ORDER. Thus "none" does not prove that the login works.
router_skip_line() {
    if lines=$(compose logs --since "$2" --no-log-prefix codex-router 2>/dev/null); then
        if printf '%s\n' "$lines" | grep -q -F "$1 skipped"; then echo yes; else echo none; fi
    else
        echo unknown
    fi
}
skip_note="The router logs a skip only on a request, and never for the last account of its order."

# The user must be able to use the state directory of each account.
for n in $accounts; do
    dir=state/codex$n
    if [ ! -d "$dir" ]; then
        echo "The directory $PWD/$dir does not exist. Run the script in the checkout that runs the services." >&2
        exit 2
    fi
    if [ ! -r "$dir" ] || [ ! -x "$dir" ] || { [ "$check" = 0 ] && [ ! -w "$dir" ]; }; then
        echo "The user $(id -un) cannot use the directory $PWD/$dir. Run the script as root or as the owner of the directory." >&2
        exit 2
    fi
done
command -v python3 >/dev/null 2>&1 || { echo "python3 is not available." >&2; exit 2; }

if [ "$check" = 1 ]; then
    incomplete=0
    for n in $accounts; do
        service=codex$n
        file=state/$service/auth.json
        set -- $(service_state "$service")
        status=$1 health=$2
        if [ -f "$file" ]; then
            if [ -r "$file" ]; then
                set -- $(token_state "$file")
            else
                set -- unreadable -
            fi
            present=yes mode=$(mode_of "$file") login=$1 hours=$2
        else
            present=no mode=- login=missing hours=-
        fi
        [ "$login" = complete ] || incomplete=1
        [ "$hours" = - ] || hours=${hours}h
        echo "$service: service=$status health=$health file=$present mode=$mode login=$login expires_in=$hours router_skip_line_2m=$(router_skip_line "$service" 2m)"
    done
    echo "Note: $skip_note \"none\" is not a proof of a login that works."
    exit "$incomplete"
fi

[ -f scripts/login-codex.sh ] || { echo "scripts/login-codex.sh is missing." >&2; exit 2; }

up_command="docker compose -f compose.yaml -f compose.codex.yaml up -d"

# State of the account in work. The EXIT trap uses it.
#   phase     empty: no account in work
#             stopping: the stop was sent; the token file is not changed
#             moved: the service is stopped and the old token file is aside
#             verified: the new token file passed the checks
#   login     none: the login did not start; started: it started
phase= service= dir= file= backup= had_file=0 was_running=0 reason= login_pid=
login=none oneoff_before= oneoff_known=0 tty_state=

# Print the ids of the one-off containers ("docker compose run") of the service
# in this checkout, also the ones that exited.
oneoff_ids() {
    "$docker" ps -a -q --filter label=com.docker.compose.oneoff=True \
        --filter "label=com.docker.compose.service=$service" \
        --filter "label=com.docker.compose.project.working_dir=$PWD" 2>/dev/null
}

# End a login that still runs: its processes and its one-off container.
# Only a container that did not exist before the login is removed.
kill_login() {
    [ "$login" = started ] || return 0
    if [ -n "$login_pid" ]; then
        for child in $(ps -A -o pid= -o ppid= 2>/dev/null | awk -v parent="$login_pid" '$2 == parent { print $1 }'); do
            kill -TERM "$child" 2>/dev/null
        done
        kill -TERM "$login_pid" 2>/dev/null
        login_pid=
    fi
    # docker compose run can leave the terminal in raw mode when it is killed.
    [ -z "$tty_state" ] || stty "$tty_state" 2>/dev/null
    if [ "$oneoff_known" = 0 ] || ! ids=$(oneoff_ids); then
        echo "$service: docker ps failed, so the script did not look for a login container. Look with: docker ps -a --filter label=com.docker.compose.oneoff=True" >&2
        return 0
    fi
    for id in $ids; do
        case " $(echo $oneoff_before) " in
            *" $id "*) ;;
            *) "$docker" rm -f "$id" >/dev/null 2>&1 ||
                echo "$service: the login container $id is still there. Remove it before the next login: docker rm -f $id" >&2 ;;
        esac
    done
}

start_again() {
    if [ "$was_running" = 0 ]; then
        echo "$service: the service did not run before, so it stays stopped." >&2
    elif compose up -d "$service" >/dev/null 2>&1; then
        echo "$service: the service is started again." >&2
    else
        echo "$service: the service did not start. Run: cd $PWD && $up_command $service" >&2
    fi
}

# Runs at an exit with an account in work. It leaves the account in a usable state.
finish() {
    trap '' HUP INT TERM
    set +e
    echo "$service: FAILED: ${reason:-interrupted}" >&2
    case "$phase" in
        stopping)
            echo "$service: the token file is not changed." >&2
            start_again ;;
        moved)
            # The script stopped the service and did not start it, so no stop is necessary here.
            kill_login
            if [ "$had_file" = 0 ]; then
                # No old file: remove the file that the failed login made.
                rm -f "$file"
                echo "$service: there was no old token file. The token directory has no auth.json." >&2
                start_again
            elif [ -f "$backup" ] && mv -f "$backup" "$file"; then
                echo "$service: the old token file is back at $file." >&2
                start_again
            elif [ ! -e "$backup" ] && [ "$login" = none ] && [ -f "$file" ]; then
                # The signal came before the move: the old file is still in place.
                echo "$service: the token file is not changed." >&2
                start_again
            else
                echo "$service: the old token file is NOT back. The service stays stopped. Run:" >&2
                echo "  cd $PWD && mv $backup $file && $up_command $service" >&2
            fi ;;
        verified)
            # Never put the old file over a complete new login.
            echo "$service: the new login is complete and stays at $file." >&2
            [ "$had_file" = 0 ] || echo "$service: the old token file is still at $backup." >&2
            if compose up -d "$service" >/dev/null 2>&1; then
                echo "$service: the start of the service was sent. Look at it: sh scripts/reauth-codex.sh ${service#codex} --check" >&2
            else
                echo "$service: the service did not start. Run: cd $PWD && $up_command $service" >&2
            fi ;;
    esac
}

on_exit() {
    code=$?
    if [ -n "$phase" ]; then
        finish
        [ "$code" -ne 0 ] || code=1
    fi
    exit "$code"
}
trap on_exit EXIT
# A signal handler does not end the script; exit runs the EXIT trap.
# Without a handler, sh does not run the EXIT trap for HUP.
trap 'reason="interrupted (HUP)"; exit 129' HUP
trap 'reason="interrupted (INT)"; exit 130' INT
trap 'reason="interrupted (TERM)"; exit 143' TERM

fail() {
    reason=$1
    exit 1
}

# Remove old backups after a good run. Only names that this script writes count,
# and the backup of this run always stays. With it, at most 3 backups stay.
prune_backups() {
    keep=3
    current=${backup##*/}
    [ -z "$current" ] || keep=2
    ls -1 "$dir" | grep -x 'auth\.json\.before-reauth-[0-9]\{8\}T[0-9]\{6\}Z' | sort -r |
        { if [ -n "$current" ]; then grep -v -x -F "$current"; else cat; fi; } | sed "1,${keep}d" |
        while read -r old; do rm -f -- "$dir/$old"; done
}

reauth() {
    n=$1
    service=codex$n
    dir=state/$service
    file=$dir/auth.json
    backup= had_file=0 login_pid= login=none oneoff_before= oneoff_known=0 tty_state=

    echo "Plan for $service:"
    echo "  1. Stop the service $service. The other account services stay up."
    echo "  2. Move $file to $file.before-reauth-<UTC time>."
    echo "  3. Run sh scripts/login-codex.sh $n. It shows a URL and a device code."
    echo "  4. Check the new token file and start $service."
    echo "  5. Wait up to $timeout s for a healthy service and read the router log."
    echo "  The service $service is away from step 1 until it is healthy in step 5."
    echo "  A failure or a signal before the new token file is complete puts the old token file back."
    echo "  A complete new token file always stays."
    if [ "$yes" = 0 ]; then
        printf 'Continue with %s? [y/N] ' "$service"
        read -r answer || answer=
        case "$answer" in
            y|Y|yes) ;;
            *) echo "$service: not changed."; return 1 ;;
        esac
    fi

    # After a failure the service starts again, except when it was stopped before.
    # "restarting" and an unknown state count as a service that ran.
    set -- $(service_state "$service")
    case "$1" in
        exited|created|dead|absent) was_running=0 ;;
        *) was_running=1 ;;
    esac

    phase=stopping
    compose stop "$service" || fail "the service did not stop"

    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    owner=$(owner_of "$dir")
    if [ -f "$file" ]; then
        owner=$(owner_of "$file")
        [ ! -e "$file.before-reauth-$stamp" ] || fail "the backup $file.before-reauth-$stamp exists; start again in one second"
        # Set the state before the move, so that a signal during the move restores.
        backup=$file.before-reauth-$stamp had_file=1
        phase=moved
        if ! mv "$file" "$backup"; then
            if [ -f "$file" ] && [ ! -e "$backup" ]; then
                backup= had_file=0
                phase=stopping
            fi
            fail "the token file did not move"
        fi
        chmod 600 "$backup" || fail "the backup did not get mode 0600"
        echo "$service: the old token file is at $backup."
    else
        phase=moved
        echo "$service: no old token file."
    fi

    echo "$service: the login starts. Open the URL with the ChatGPT account of $service and enter the code."
    # The login runs in the background, so that a signal does not wait for its end.
    # File descriptor 3 keeps the input of the script for the login.
    if oneoff_before=$(oneoff_ids); then oneoff_known=1; fi
    if [ -t 0 ]; then tty_state=$(stty -g 2>/dev/null) || tty_state=; fi
    login=started
    exec 3<&0
    sh scripts/login-codex.sh "$n" <&3 &
    login_pid=$!
    exec 3<&-
    login_status=0
    wait "$login_pid" || login_status=$?
    login_pid=
    [ "$login_status" = 0 ] || fail "the login did not complete (exit $login_status)"

    [ -f "$file" ] || fail "the login made no token file"
    [ "$(mode_of "$file")" = 600 ] || fail "the new token file does not have mode 0600"
    [ "$(owner_of "$file")" = "$owner" ] || fail "the new token file has another owner than $owner"
    set -- $(token_state "$file")
    [ "$1" = complete ] || fail "the new token file is $1: it needs access_token and account_id"
    phase=verified

    since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    compose up -d "$service" || fail "the service did not start"
    start=$(date +%s)
    while :; do
        set -- $(service_state "$service")
        waited=$(($(date +%s) - start))
        [ "$2" != healthy ] || break
        [ "$waited" -lt "$timeout" ] || fail "the service is not healthy after $timeout s (state $1, health $2)"
        sleep "$poll"
    done

    phase=
    prune_backups
    skip=$(router_skip_line "$service" "$since")
    if [ "$skip" = yes ]; then
        echo "$service: WARNING: new login, service healthy, but the router logged \"$service skipped\" after the login. The old token file stays at ${backup:-(none)}."
        return 1
    fi
    case "$skip" in
        none) observed="no skip line in the router log since the login" ;;
        *) observed="the router log was not available" ;;
    esac
    echo "$service: OK: new login, service healthy after $waited s; $observed. Not a proof: $skip_note"
}

for n in $accounts; do
    reauth "$n" || exit 1
done
