# AGENTS.md

## What this is

Single-file script `uxplay_presence.py`: spawns uxplay itself as an audio-only AirPlay server (`-p`, `-vs 0`, `-md`, `-ca`), watches the metadata file uxplay writes, mirrors Track / Artist / Album plus elapsed/remaining time to Discord Rich Presence (pypresence IPC), and serves a "now playing" web page (`/`, `/state` JSON, `/art` JPEG, `/config` GET/POST for the presence-line mapping) on `127.0.0.1:8080` (`--ui-port`, `--no-ui` to disable), auto-opened in the browser (`--no-browser` to disable). No packages, no test suite, no lint/CI tooling — don't invent commands for these.

## Environment

- Python 3.14 venv at `.venv/` (repo has no pyproject; deps are exact-pinned in `requirements.txt`: pypresence, watchdog).
- Install: `.venv/bin/pip install -r requirements.txt`

## Verification

- Syntax check: `.venv/bin/python -m py_compile uxplay_presence.py`
- Behavior check: `.venv/bin/python uxplay_presence.py --dry-run` — logs payloads instead of talking to Discord, and does not require a client ID. Still spawns uxplay.
- Without an AirPlay device: `--dry-run --no-uxplay` plus a hand-written `Key: value` meta file (create the dir first) exercises watcher, payloads, and web UI; write/delete the file to test update/clear paths.

## Runtime prerequisites (not code)

- Requires `uxplay` on PATH (the script spawns it; override session with `--port` / `--name`; `--no-uxplay` attaches to an externally started uxplay instead).
- Discord desktop app must be running for IPC; client ID comes from `--client-id` or `$DISCORD_CLIENT_ID` (no bot token needed).

## Non-obvious constraints when editing

- Discord rate limits presence updates: keep the `--min-interval` throttle (default 15s) for regular track updates and the identity-based "payload unchanged, skip" logic in `PresenceBridge` (compares title/artist/album/image only, deliberately excluding recomputed `start`/`end` timestamps). Clears, resume-restores, and config-change refreshes deliberately use a 2s floor instead (`RESUME_THROTTLE`) so they aren't delayed by the 15s throttle — Discord tolerates these small bursts (~5 updates/20s).
- Playback pause/stream-end detection: progress lines stop when audio stops, so the worker clears presence when the last snapshot is older than `PAUSE_TIMEOUT` (5s) and auto-restores it when fresh progress reappears. Impossible in mirror mode (no progress lines at all) — there, clearing still happens via file deletion or uxplay rewriting the file to "no data".
- Discord field limit is 128 chars (`clip()`); values shorter than 2 chars are dropped.
- Duration/position do NOT come from the `-md` file (only string tags there). They are parsed from uxplay's stdout line `audio progress (min:sec): P:SS; remaining: R:SS; track length D:SS` (`\r`-terminated, ~1×/sec, minutes can exceed 59). That line only exists in audio-only ALAC streaming — mirror-mode AAC disables it, in which case the app degrades to no timestamps (worker waits up to 3s per track change, gives up permanently after 3 misses).
- Track-change updates wait for a *fresh* progress snapshot (max 8s old) so timestamps belong to the new track, not the previous one.
- Metadata file format is `Key: value` lines, keys lowercased; `title`, `artist`, `album`, `album artist`, `genre` are mappable to the four Discord presence slots (`name`/`details`/`state`/`large_text` — `large_text` is the image caption Discord renders as a 4th line, never auto-copied from `state`), plus a fixed-text option `apple music` that sends the literal "Apple Music". Defaults: `name`→apple music, `details`→title, `state`→artist, `large_text`→album. A fifth key `status_display` (`name`/`details`/`state`, default `state`) picks which line the member-list badge shows ("Listening to <that line>"; the card's first line always follows `name`, with fallback to `name` when the chosen line is absent from the payload). Configured in the web UI, persisted in `~/.config/uxplay-presence/config.json` (`--config` to override); a saved config missing the `large_text` key is treated as legacy and replaced by the new defaults on load (logged). Presence and UI clear when the file is deleted/renamed (uxplay deletes it on exit) or has no title — the watcher handles modified/created/moved/deleted events.
- Cover art: iTunes Search API first (artworkUrl100 rewritten to 1000x1000), fallback to catbox.moe upload; both cached in memory. The web UI always serves the local file from uxplay via `/art`.
