#!/usr/bin/env bash
# Launch AirPlayPresence with default parameters.
#
# Usage:
#   ./run.sh                 - use the token stored in .client-id (repo root)
#   ./run.sh <CLIENT_ID>     - store the token in .client-id and run
#   ./run.sh --client-id X   - same as above
#   ./run.sh -- <extra args> - extra args are passed to the program
# Useful extra args (see .venv/bin/python -m airplay_presence -h):
#   --log [FILE]  also write the full DEBUG log (exceptions, Discord payloads,
#                 timers, all uxplay output) to a file (default .run/airplay-presence.log)
#
# Examples:
#   ./run.sh -- --log              # file log; value printed to the console at startup
#   ./run.sh -- --log /tmp/dbg.log
set -euo pipefail
cd "$(dirname "$0")"

TOKEN_FILE=".client-id"
PID_DIR=".run"

if [[ $# -gt 0 ]]; then
    # First argument may be the Discord client ID (it is stored, not forwarded).
    if [[ $1 == "--client-id" ]]; then
        token=$2
        shift 2
    elif [[ $1 == "--" ]]; then
        shift
    elif [[ $1 != -* ]]; then
        token=$1
        shift
    fi
fi

if [[ -n ${token:-} ]]; then
    umask 077
    printf '%s\n' "$token" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "stored Discord client ID in $PWD/$TOKEN_FILE"
fi

if [[ -z ${DISCORD_CLIENT_ID:-} && -f $TOKEN_FILE ]]; then
    stored=$(< "$TOKEN_FILE")
    if [[ -n $stored ]]; then
        export DISCORD_CLIENT_ID="$stored"
    fi
fi

if [[ -z ${DISCORD_CLIENT_ID:-} && ! -f $TOKEN_FILE ]]; then
    echo "error: no Discord client ID" >&2
    echo "  pass one:   ./run.sh <CLIENT_ID>" >&2
    echo "  or set env: DISCORD_CLIENT_ID=<CLIENT_ID> ./run.sh" >&2
    echo "  or store:   echo <CLIENT_ID> > $PWD/$TOKEN_FILE" >&2
    exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
    echo "creating virtualenv (.venv) ..."
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e .
else
    .venv/bin/pip install --quiet -e . >/dev/null 2>&1 || true
fi

mkdir -p "$PID_DIR"
echo $$ > "$PID_DIR/launcher.pid"

exec .venv/bin/python -m airplay_presence "$@"
