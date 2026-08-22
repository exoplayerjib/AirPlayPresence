#!/usr/bin/env python3
"""Show uxplay AirPlay Now-Playing metadata as Discord Rich Presence.

Run uxplay with metadata and cover-art export, e.g.:

    uxplay -md /tmp/uxplay/meta.txt -ca /tmp/uxplay/image.jpeg

This script watches the metadata file and mirrors Track / Artist / Album
to Discord through the local IPC socket (SET_ACTIVITY). Cover art is
resolved via the iTunes Search API (Apple's CDN, which Discord fetches
reliably); if the track is not found there, the local artwork file is
uploaded to catbox.moe instead.

One-time Discord application setup:
  1. Open https://discord.com/developers/applications -> New Application.
  2. Choose a name; it shows on your profile as "Playing <name>".
  3. Copy the Application ID from General Information and pass it via
     --client-id or the DISCORD_CLIENT_ID environment variable.
No bot or OAuth configuration is required.

Usage:
    uxplay_presence.py --client-id <APPLICATION ID>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import re
import signal
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pypresence import ActivityType, Presence
from pypresence.exceptions import (
    DiscordError,
    InvalidID,
    PipeClosed,
    PyPresenceException,
    ResponseTimeout,
    ServerError,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

FIELD_LIMIT = 128
UPLOAD_URL = "https://catbox.moe/user/api.php"

log = logging.getLogger("uxplay-presence")


def clean_value(raw: str) -> str:
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Cf").strip()


def clip(value: str | None) -> str | None:
    value = (value or "").strip()
    if len(value) < 2:
        return None
    return value[:FIELD_LIMIT]


def parse_metadata(path: Path, retries: int = 3, delay: float = 0.25) -> dict | None:
    for attempt in range(retries):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("cannot read %s (%s), retry %d/%d", path, exc, attempt + 1, retries)
            time.sleep(delay)
            continue
        fields: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip().lower()] = clean_value(value)
        return fields
    log.error("giving up on %s for now", path)
    return None


class ImageUploader:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._cache: dict[str, str] = {}

    def url_for(self, path: Path) -> str | None:
        if not self.enabled:
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("cover art unreadable: %s", exc)
            return None
        digest = hashlib.sha256(data).hexdigest()
        cached = self._cache.get(digest)
        if cached:
            return cached
        url = self._upload(data)
        if url:
            self._cache[digest] = url
        return url

    def _upload(self, data: bytes) -> str | None:
        boundary = uuid.uuid4().hex
        body = b"".join((
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="reqtype"\r\n\r\n',
            b"fileupload",
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="fileToUpload"; filename="cover.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ))
        request = urllib.request.Request(
            UPLOAD_URL,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "uxplay-discord-presence/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                url = response.read().decode("utf-8", "replace").strip()
        except (urllib.error.URLError, OSError) as exc:
            log.warning("cover art upload failed: %s", exc)
            return None
        if not url.startswith("https://"):
            log.warning("unexpected upload response: %r", url)
            return None
        log.info("uploaded cover art: %s", url)
        return url


def norm_text(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value.lower()) if ch.isalnum()
    )


class ArtworkResolver:
    def __init__(self, uploader: ImageUploader):
        self.uploader = uploader
        self._cache: dict[tuple[str, str], str | None] = {}

    def url_for(self, meta: dict, image_path: Path) -> str | None:
        title = meta.get("title") or ""
        artist = meta.get("artist") or ""
        key = (title, artist)
        if key in self._cache:
            return self._cache[key]
        url = self._lookup_itunes(title, artist)
        if not url:
            url = self.uploader.url_for(image_path)
        self._cache[key] = url
        return url

    def _lookup_itunes(self, title: str, artist: str) -> str | None:
        if not title:
            return None
        term = f"{title} {artist}".strip()
        params = urllib.parse.urlencode({"term": term, "entity": "song", "limit": "5"})
        request = urllib.request.Request(
            f"https://itunes.apple.com/search?{params}",
            headers={"User-Agent": "uxplay-discord-presence/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("iTunes lookup failed: %s", exc)
            return None
        want_title = norm_text(title)
        artist_tokens = {t for t in re.split(r"\W+", artist.lower()) if len(t) >= 3}
        for result in data.get("results", []):
            if norm_text(result.get("trackName", "")) != want_title:
                continue
            result_artist = result.get("artistName", "").lower()
            if artist_tokens and not any(tok in result_artist for tok in artist_tokens):
                continue
            art = result.get("artworkUrl100")
            if art:
                url = art.replace("100x100bb", "1000x1000bb")
                log.info("artwork from iTunes: %s", url)
                return url
        log.info("no iTunes match for %r, falling back to catbox upload", title)
        return None


class PresenceBridge:
    def __init__(self, client_id: str, min_interval: float, dry_run: bool):
        self.client_id = client_id
        self.min_interval = min_interval
        self.dry_run = dry_run
        self._rpc: Presence | None = None
        self._last_send = 0.0
        self._last_payload: dict | None = None
        self._cleared = True

    def connect(self) -> bool:
        if self.dry_run:
            log.info("dry run: skipping Discord connection")
            return True
        backoff = 5.0
        while True:
            try:
                rpc = Presence(self.client_id)
                rpc.connect()
            except InvalidID:
                log.error(
                    "Discord rejected client ID %r; create an application at "
                    "https://discord.com/developers/applications and use its Application ID",
                    self.client_id,
                )
                return False
            except PyPresenceException as exc:
                log.warning("Discord not reachable (%s), retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            self._rpc = rpc
            self._last_send = 0.0
            log.info("connected to Discord IPC")
            return True

    def update(self, payload: dict) -> None:
        if not self._cleared and payload == self._last_payload:
            log.debug("payload unchanged, skipping")
            return
        if self._send("update", payload):
            self._last_payload = payload
            self._cleared = False

    def clear(self) -> None:
        if self._cleared:
            return
        if self._send("clear", None):
            self._last_payload = None
            self._cleared = True

    def close(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None

    def _throttle(self) -> None:
        wait = self._last_send + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _send(self, op: str, payload: dict | None) -> bool:
        if self.dry_run:
            log.info("dry-run %s: %s", op, payload)
            self._last_send = time.monotonic()
            return True
        for _ in range(4):
            self._throttle()
            try:
                if op == "update":
                    self._rpc.update(**payload)
                else:
                    self._rpc.clear()
                self._last_send = time.monotonic()
                return True
            except ServerError as exc:
                if "rate" in str(exc).lower():
                    log.warning("rate limited by Discord, waiting %.0fs", self.min_interval)
                    time.sleep(self.min_interval)
                    continue
                log.error("Discord rejected %s: %s", op, exc)
                return False
            except (PipeClosed, ResponseTimeout, OSError) as exc:
                log.warning("Discord connection lost (%s), reconnecting", exc)
                self._reconnect()
                continue
            except PyPresenceException as exc:
                log.error("presence %s failed: %s", op, exc)
                return False
        log.error("giving up on presence %s after repeated failures", op)
        return False

    def _reconnect(self) -> None:
        self.close()
        self._last_payload = None
        self._cleared = True
        backoff = 2.0
        while True:
            try:
                rpc = Presence(self.client_id)
                rpc.connect()
            except PyPresenceException as exc:
                log.warning("reconnect failed (%s), retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            self._rpc = rpc
            log.info("reconnected to Discord IPC")
            return


class Debouncer:
    def __init__(self, delay: float, submit):
        self.delay = delay
        self.submit = submit
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def schedule(self, job: str) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.submit, args=(job,))
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class MetaFileHandler(FileSystemEventHandler):
    def __init__(self, filename: str, debouncer: Debouncer):
        self.filename = filename
        self.debouncer = debouncer

    def _matches(self, path: str) -> bool:
        return Path(path).name == self.filename

    def on_modified(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self.debouncer.schedule("update")

    def on_created(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self.debouncer.schedule("update")

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        if self._matches(event.dest_path):
            self.debouncer.schedule("update")
        elif self._matches(event.src_path):
            self.debouncer.schedule("clear")

    def on_deleted(self, event) -> None:
        if not event.is_directory and self._matches(event.src_path):
            self.debouncer.schedule("clear")


def build_payload(meta: dict, image_url: str | None) -> dict:
    payload = {"activity_type": ActivityType.LISTENING}
    name = clip(meta.get("title"))
    details = clip(meta.get("artist"))
    state = clip(meta.get("album"))
    if name:
        payload["name"] = name
    if details:
        payload["details"] = details
    if state:
        payload["state"] = state
        payload["large_text"] = state
    if image_url:
        payload["large_image"] = image_url
    return payload


def run_worker(
    args: argparse.Namespace,
    meta_path: Path,
    image_path: Path,
    bridge: PresenceBridge,
    artwork: ArtworkResolver,
    jobs: "queue.Queue[str]",
    stop_event: threading.Event,
    failed: threading.Event,
) -> None:
    if not bridge.connect():
        failed.set()
        stop_event.set()
        return
    while not stop_event.is_set():
        try:
            job = jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            while True:
                job = jobs.get_nowait()
        except queue.Empty:
            pass
        if job == "clear":
            log.info("metadata file gone or empty, clearing presence")
            bridge.clear()
            continue
        meta = parse_metadata(meta_path)
        if meta is None:
            continue
        if not meta.get("title"):
            log.info("no track in metadata, clearing presence")
            bridge.clear()
            continue
        payload = build_payload(meta, artwork.url_for(meta, image_path))
        log.info(
            "now playing: %s | %s | %s",
            meta.get("title"),
            meta.get("artist", "?"),
            meta.get("album", "?"),
        )
        bridge.update(payload)
    bridge.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror uxplay AirPlay Now-Playing metadata to Discord Rich Presence."
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("DISCORD_CLIENT_ID"),
        help="Discord application ID (defaults to $DISCORD_CLIENT_ID)",
    )
    parser.add_argument("--meta-file", default="/tmp/uxplay/meta.txt")
    parser.add_argument("--image-file", default="/tmp/uxplay/image.jpeg")
    parser.add_argument("--no-image", action="store_true", help="do not upload cover art")
    parser.add_argument("--debounce", type=float, default=0.5, help="seconds to wait after the last file event")
    parser.add_argument("--min-interval", type=float, default=15.0, help="minimum seconds between Discord updates")
    parser.add_argument("--dry-run", action="store_true", help="log payloads instead of talking to Discord")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not args.client_id and not args.dry_run:
        parser.error("--client-id (or DISCORD_CLIENT_ID) is required")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    meta_path = Path(args.meta_file)
    image_path = Path(args.image_file)
    watch_dir = meta_path.parent
    while not watch_dir.is_dir():
        log.info("waiting for %s to appear (start uxplay with -md %s)", watch_dir, meta_path)
        time.sleep(2)

    bridge = PresenceBridge(args.client_id or "", args.min_interval, args.dry_run)
    artwork = ArtworkResolver(ImageUploader(not args.no_image))
    jobs: "queue.Queue[str]" = queue.Queue()
    stop_event = threading.Event()
    failed = threading.Event()
    debouncer = Debouncer(args.debounce, jobs.put)

    worker = threading.Thread(
        target=run_worker,
        args=(args, meta_path, image_path, bridge, artwork, jobs, stop_event, failed),
        daemon=True,
    )
    worker.start()

    observer = Observer()
    observer.schedule(MetaFileHandler(meta_path.name, debouncer), str(watch_dir), recursive=False)
    observer.start()
    log.info("watching %s", meta_path)
    jobs.put("update")

    def shutdown(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        debouncer.cancel()
        observer.stop()
        observer.join(timeout=5)
        stop_event.set()
        worker.join(timeout=5)
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
