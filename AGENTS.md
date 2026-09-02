# AGENTS.md

## What this is

Project **AirPlayPresence** (GPLv3): single-file script `airplay_presence.py` that spawns `uxplay` itself as a headless audio-only AirPlay server (`-p`, `-vs 0`, `-md`, `-ca`), watches the metadata/cover files uxplay writes, and feeds three outputs: Discord Rich Presence (pypresence IPC, with elapsed/remaining timers), an MPRIS player on D-Bus, and a local web UI (`/`, `/state`, `/art`, `/config` on `127.0.0.1:8080`). No test suite, no lint/CI — don't invent commands for these.

## Environment

- Python 3.10+ (venv at `.venv/`, Python 3.14 here); deps exact-pinned in `requirements.txt`: pypresence 4.6.2, watchdog 6.0.0, dbus-next 0.2.3.
- Install: `.venv/bin/pip install -r requirements.txt`
- Regenerating the venv after moving the repo folder is required (venv scripts embed absolute paths).

## Verification

- Syntax: `.venv/bin/python -m py_compile airplay_presence.py`
- Behavior: `.venv/bin/python airplay_presence.py --dry-run` — logs payloads instead of talking to Discord (no client ID needed), still spawns uxplay and honors the send throttle.
- Without an AirPlay device: `--dry-run --no-uxplay` plus a hand-written `Key: value` meta file exercises watcher/payloads/UI. For seeks/timers, shim a fake `uxplay` on PATH that writes the meta file and prints the progress line (see below) — that runs the whole pipeline.

## Runtime prerequisites (not code)

- `uxplay` binary on PATH; Discord desktop app running; client ID via `--client-id` or `$DISCORD_CLIENT_ID`; a D-Bus session bus for MPRIS (missing → logged and disabled).
- AirPlay ports must be open (UDP 5353, TCP 7100/7000/7001, UDP 6000/6001/7011 — see README firewall section).

## Non-obvious constraints when editing

- **Discord sends run in a sender thread** inside `PresenceBridge` (FIFO queue): `update()`/`clear()` only enqueue; identity-skip, the 15s `--min-interval` throttle, retries, and reconnects live there. The worker never blocks on Discord — pause watchdog, clears, and resumes stay real-time. Clears/resume/refresh/seek updates use the `RESUME_THROTTLE` (2s) floor; dry-run honors the throttle too.
- **Identity-skip** compares name/details/state/large_image plus a `has_timer` marker (timer-less → timed transitions re-send) and deliberately excludes recomputed `start`/`end` values. Bypass it only via `force=True` (seek updates use this).
- **Seek detection**: `ProgressState.set()` bumps `seek_seq` when a print jumps > `SEEK_FORWARD_JUMP` (4s) forward or > `SEEK_BACKWARD_JUMP` (2s) backward vs the previous print; the worker's idle tick then re-sends with `force=True`. Normal +1–2s steps never trip it. Fast-forward produces no metadata event — without this, the timer goes stale.
- **Duration/position come from uxplay's stdout**, not the `-md` file: line `audio progress (min:sec): P:SS; remaining: R:SS; track length D:SS`, `\r`-terminated, ~1×/sec, minutes can exceed 59. Only exists for audio-only ALAC streaming (mirror-mode AAC disables it → degrade to no timestamps). That line is logged at INFO (the log's heartbeat); other uxplay output is DEBUG.
- **`\r`-newline trap**: Python's universal-newline reader can't return a `\r`-terminated line until the next stdout byte arrives, so the old track's final progress print lingers unresolved across track gaps. This is why the new-track signature (`new_track_signature`) requires duration ≠ prior or position ≤ prior's, and why resume requires raw position advancement — don't relax those.
- **Pause watchdog**: presence clears when prints go silent > `PAUSE_TIMEOUT` (5s) and auto-restores on resume. Mirror mode has no prints at all — clearing there relies on file deletion or uxplay rewriting the file to "no data".
- **Discord field limit** is 128 chars (`clip()`); values < 2 chars are dropped.
- **Metadata file**: `Key: value` lines, keys lowercased. `title/artist/album/album artist/genre` map to the four presence slots (`name`/`details`/`state`/`large_text`) + fixed-text option `apple music`; `status_display` picks the member-list badge line. Saved config at `~/.config/airplay-presence/config.json` (`--config` to override; legacy `uxplay-presence` dir auto-migrates).
- **Cover art**: iTunes Search first (artworkUrl100 → 1000x1000), then catbox.moe upload. `valid_art_bytes` gates uploads (≥ `MIN_ART_BYTES`, JPEG SOI/EOI) because uxplay writes a 95-byte 1×1 PNG placeholder into the `-ca` file on stream changes and real art often arrives after the metadata event — `wait_valid_art` polls up to `ART_SETTLE_TIMEOUT` (5s). Failed lookups are not cached (they retry on later events). `/art` applies the same gate.
- **MPRIS**: dbus-next asyncio loop in a daemon thread; publishes metadata/status/position. Control methods raise `NotSupported`, `CanControl=false` — uxplay's raop is receive-only (`/rate`/`/setProperty` are client→server), the phone cannot be commanded. dbus-next is imported defensively: absent library or bus just logs and disables. GNOME queries `DesktopEntry` on the root interface — keep it present.
- **Discord rate limits**: ~5 updates/20s. The 15s interval is the regular cadence; clears/resume/refresh/seek use the 2s floor. Don't remove either.
