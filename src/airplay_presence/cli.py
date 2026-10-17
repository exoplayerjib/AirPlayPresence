from __future__ import annotations

import argparse
import os

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run uxplay and mirror its AirPlay Now-Playing metadata to Discord Rich Presence, with a local web UI."
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("DISCORD_CLIENT_ID"),
        help="Discord application ID (defaults to $DISCORD_CLIENT_ID)",
    )
    parser.add_argument("--meta-file", default="/tmp/uxplay/meta.txt")
    parser.add_argument("--image-file", default="/tmp/uxplay/image.jpeg")
    parser.add_argument("--no-image", action="store_true", help="do not upload cover art")
    parser.add_argument("--itunes-art", action="store_true", help="look up cover art on iTunes before uploading (upload-only by default)")
    parser.add_argument(
        "--spotify-client-id",
        default=os.environ.get("SPOTIFY_CLIENT_ID"),
        help="Spotify app client ID for the cover-art fallback between iTunes and upload (defaults to $SPOTIFY_CLIENT_ID)",
    )
    parser.add_argument(
        "--spotify-client-secret",
        default=os.environ.get("SPOTIFY_CLIENT_SECRET"),
        help="Spotify app client secret for the cover-art fallback (defaults to $SPOTIFY_CLIENT_SECRET)",
    )
    parser.add_argument("--port", type=int, default=None, help="AirPlay TCP port for uxplay (default: uxplay's legacy port set)")
    parser.add_argument("--name", default="AirPlayPresence", help="AirPlay server name shown on clients (default: AirPlayPresence)")
    parser.add_argument("--ui-port", type=int, default=8080, help="port for the local web UI (default 8080)")
    parser.add_argument("--no-ui", action="store_true", help="disable the local web UI")
    parser.add_argument("--no-mpris", action="store_true", help="do not expose an MPRIS media player on D-Bus")
    parser.add_argument("--no-browser", action="store_true", help="do not open the web UI in a browser on startup")
    parser.add_argument("--config", default=None, help="path for the presence-lines config (default ~/.config/airplay-presence/config.json)")
    parser.add_argument("--no-uxplay", action="store_true", help="do not spawn uxplay; watch files written by an externally started uxplay")
    parser.add_argument("--debounce", type=float, default=1.5, help="seconds to wait after the last metadata file event; rapid track changes within this window collapse into one update")
    parser.add_argument("--min-interval", type=float, default=5.0, help="minimum seconds between Discord updates; the presence is also re-sent at this cadence even when unchanged")
    parser.add_argument("--dry-run", action="store_true", help="log payloads instead of talking to Discord")
    parser.add_argument("--no-tui", action="store_true", help="plain log output instead of the curses terminal UI (automatic when stdout is not a terminal)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--log",
        nargs="?",
        const=".run/airplay-presence.log",
        default=None,
        metavar="FILE",
        help="also write the full DEBUG-level log (exceptions, every Discord payload, timers, all uxplay output) to this file (default when the flag is bare: .run/airplay-presence.log)",
    )
    args = parser.parse_args()
    if not args.client_id and not args.dry_run:
        parser.error("--client-id (or DISCORD_CLIENT_ID) is required")
    return args
