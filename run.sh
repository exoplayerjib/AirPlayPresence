#!/usr/bin/env bash
# Launch AirPlayPresence with default parameters.
#
# Usage:
#   ./run.sh                 - use the token stored in .client-id (repo root)
#   ./run.sh <CLIENT_ID>     - store the token in .client-id and run
#   ./run.sh --client-id X   - same as above
#   ./run.sh --spotify-id ID --spotify-secret SECRET
#                            - store Spotify credentials in .spotify-client-id /
#                              .spotify-client-secret and enable the Spotify
#                              cover-art fallback (env SPOTIFY_CLIENT_ID /
#                              SPOTIFY_CLIENT_SECRET override the stored files)
#   ./run.sh -- <extra args> - extra args are passed to the program
# Useful extra args (see .venv/bin/python -m airplay_presence -h):
#   --log [FILE]  also write the full DEBUG log (exceptions, Discord payloads,
#                 timers, all uxplay output) to a file (default .run/airplay-presence.log)
#   --itunes-art  look up cover art on iTunes before uploading
#
# Examples:
#   ./run.sh -- --log              # file log; value printed to the console at startup
#   ./run.sh -- --log /tmp/dbg.log
#   ./run.sh --spotify-id ID --spotify-secret SECRET -- --itunes-art
set -euo pipefail
cd "$(dirname "$0")"

TOKEN_FILE=".client-id"
PID_DIR=".run"

token=""
spotify_id=""
spotify_secret=""
passthrough=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --client-id)
            [[ $# -ge 2 ]] || { echo "error: --client-id needs a value" >&2; exit 1; }
            token=$2; shift 2 ;;
        --spotify-id)
            [[ $# -ge 2 ]] || { echo "error: --spotify-id needs a value" >&2; exit 1; }
            spotify_id=$2; shift 2 ;;
        --spotify-secret)
            [[ $# -ge 2 ]] || { echo "error: --spotify-secret needs a value" >&2; exit 1; }
            spotify_secret=$2; shift 2 ;;
        --)
            shift
            passthrough+=("$@")
            break ;;
        *)
            passthrough+=("$1")
            shift ;;
    esac
done

if [[ -n $token ]]; then
    umask 077
    printf '%s\n' "$token" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "stored Discord client ID in $PWD/$TOKEN_FILE"
fi
if [[ -n $spotify_id ]]; then
    umask 077
    printf '%s\n' "$spotify_id" > .spotify-client-id
    chmod 600 .spotify-client-id
    echo "stored Spotify client ID in $PWD/.spotify-client-id"
fi
if [[ -n $spotify_secret ]]; then
    umask 077
    printf '%s\n' "$spotify_secret" > .spotify-client-secret
    chmod 600 .spotify-client-secret
    echo "stored Spotify client secret in $PWD/.spotify-client-secret"
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

if [[ -z ${SPOTIFY_CLIENT_ID:-} && -f .spotify-client-id ]]; then
    stored=$(< .spotify-client-id)
    if [[ -n $stored ]]; then
        export SPOTIFY_CLIENT_ID="$stored"
    fi
fi
if [[ -z ${SPOTIFY_CLIENT_SECRET:-} && -f .spotify-client-secret ]]; then
    stored=$(< .spotify-client-secret)
    if [[ -n $stored ]]; then
        export SPOTIFY_CLIENT_SECRET="$stored"
    fi
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

exec .venv/bin/python -m airplay_presence "${passthrough[@]}"
