#!/usr/bin/env python3
"""Show uxplay AirPlay Now-Playing metadata as Discord Rich Presence, with a local web UI.

This script starts uxplay itself as an audio-only AirPlay server (no video
window), watches the metadata file and cover art it writes, mirrors
Track / Artist / Album to Discord through the local IPC socket
(SET_ACTIVITY) with elapsed / remaining time taken from uxplay's audio
progress output, and serves a "now playing" page (which also edits which
metadata field goes on each of the four Discord presence lines) at
http://127.0.0.1:<ui-port>, opened in the browser on startup.

Presence is cleared when playback pauses or the stream ends (detected via
progress-line silence) and restored automatically on resume.

One-time Discord application setup:
  1. Open https://discord.com/developers/applications -> New Application.
  2. Choose a name; it shows on your profile as "Playing <name>".
  3. Copy the Application ID from General Information and pass it via
     --client-id or the DISCORD_CLIENT_ID environment variable.
No bot or OAuth configuration is required.

Usage:
    uxplay_presence.py --client-id <APPLICATION ID>

Elapsed / remaining times require audio-only (ALAC) AirPlay streaming
(e.g. Apple Music); screen-mirroring (AAC) provides no progress data.
Pass --no-uxplay to attach to an externally started uxplay instead.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pypresence import ActivityType, Presence, StatusDisplayType
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
PAUSE_TIMEOUT = 5.0
RESUME_THROTTLE = 2.0
PROGRESS_RE = re.compile(
    r"audio progress \(min:sec\):\s*(\d+):(\d+);"
    r" remaining:\s*(\d+):(\d+);"
    r" track length (\d+):(\d+)"
)
IDENTITY_KEYS = ("name", "details", "state", "large_image", "large_text", "status_display_type")
SLOTS = ("name", "details", "state", "large_text")
SOURCE_FIELDS = ("title", "artist", "album", "album artist", "genre")
APPLE_MUSIC = "apple music"
APPLE_MUSIC_TEXT = "Apple Music"
STATUS_DISPLAY_OPTIONS = ("name", "details", "state")
STATUS_DISPLAY_MAP = {
    "name": StatusDisplayType.NAME,
    "details": StatusDisplayType.DETAILS,
    "state": StatusDisplayType.STATE,
}
DEFAULT_CONFIG = {
    "name": APPLE_MUSIC,
    "details": "title",
    "state": "artist",
    "large_text": "album",
    "status_display": "state",
}


def valid_source(value) -> bool:
    return value in SOURCE_FIELDS or value in ("none", APPLE_MUSIC)

log = logging.getLogger("uxplay-presence")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Now Playing</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:#0f1115;color:#e8eaf0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
.card{width:min(380px,92vw);padding:28px;border-radius:18px;background:#171a21;box-shadow:0 10px 40px rgba(0,0,0,.55);text-align:center}
img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;background:#20242e}
h1{font-size:1.3rem;margin:18px 0 4px;overflow-wrap:anywhere}
.artist{margin:0;color:#a3aabb;overflow-wrap:anywhere}
.album{margin:4px 0 0;color:#6b7280;font-size:.85rem;overflow-wrap:anywhere}
.paused-label{display:none;margin:14px 0 0;color:#f0b232;font-size:.85rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.bar{height:6px;border-radius:3px;background:#262b36;margin:22px 0 8px;overflow:hidden}
.bar>div{height:100%;width:0;border-radius:3px;background:#5865f2;transition:width 1s linear}
.times{display:flex;justify-content:space-between;font-size:.8rem;color:#a3aabb;font-variant-numeric:tabular-nums}
#idle p{margin:0;color:#a3aabb}
#idle .sub{color:#6b7280;font-size:.85rem;margin-top:6px}
.settings{width:min(380px,92vw);border-radius:12px;background:#171a21;color:#a3aabb;font-size:.85rem}
.settings summary{cursor:pointer;padding:12px 16px;color:#a3aabb;user-select:none}
.settings .row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 16px}
.settings select{background:#262b36;color:#e8eaf0;border:1px solid #33384a;border-radius:6px;padding:4px 8px;font-size:.85rem}
</style>
</head>
<body>
<div class="card">
  <div id="idle"><p>Nothing playing</p><p class="sub">Start an AirPlay session to this server.</p></div>
  <div id="now" style="display:none">
    <img id="art" alt="cover art">
    <p class="paused-label" id="paused">Paused</p>
    <h1 id="title"></h1>
    <p class="artist" id="artist"></p>
    <p class="album" id="album"></p>
    <div class="bar"><div id="fill"></div></div>
    <div class="times" id="times"><span id="elapsed">0:00</span><span id="total">0:00</span></div>
  </div>
</div>
<details class="settings" id="settings">
  <summary>Discord presence lines</summary>
  <div class="row"><span>First line (Listening to &hellip;)</span><select id="sel-name"></select></div>
  <div class="row"><span>Second line</span><select id="sel-details"></select></div>
  <div class="row"><span>Third line</span><select id="sel-state"></select></div>
  <div class="row"><span>Fourth line (image text)</span><select id="sel-large_text"></select></div>
  <div class="row"><span>Member list shows</span><select id="sel-status_display"></select></div>
</details>
<script>
const el = id => document.getElementById(id);
const fmt = t => { t = Math.max(0, Math.floor(t)); return Math.floor(t/60) + ":" + String(t%60).padStart(2,"0"); };
const FIELDS = [["title","Title"],["artist","Artist"],["album","Album"],["album artist","Album artist"],["genre","Genre"],["apple music","Apple Music (fixed)"],["none","(nothing)"]];
const SLOTS = [["name","sel-name"],["details","sel-details"],["state","sel-state"],["large_text","sel-large_text"]];
const DISPLAYS = [["name","First line (Apple Music)"],["details","Second line"],["state","Third line"]];
let cfg = null;
async function postConfig(next, fallback) {
  try {
    const r = await fetch("/config", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(next) });
    if (!r.ok) throw new Error("status " + r.status);
    cfg = await r.json();
    return true;
  } catch (e) { return false; }
}
for (const [slot, sel] of SLOTS) {
  const s = el(sel);
  for (const [value, label] of FIELDS) {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    s.appendChild(o);
  }
  s.addEventListener("change", async () => {
    if (!await postConfig({ ...cfg, [slot]: s.value })) s.value = cfg ? cfg[slot] : "title";
  });
}
const selD = el("sel-status_display");
for (const [value, label] of DISPLAYS) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  selD.appendChild(o);
}
selD.addEventListener("change", async () => {
  if (!await postConfig({ ...cfg, status_display: selD.value })) selD.value = cfg ? cfg.status_display : "state";
});
(async () => {
  try { cfg = await (await fetch("/config")).json(); } catch (e) { return; }
  for (const [slot, sel] of SLOTS) el(sel).value = cfg[slot] || "title";
  selD.value = cfg.status_display || "state";
})();
async function tick() {
  try {
    const s = await (await fetch("/state")).json();
    const visible = s.playing || s.paused;
    el("idle").style.display = visible ? "none" : "block";
    el("now").style.display = visible ? "block" : "none";
    if (!visible) return;
    el("paused").style.display = s.paused ? "block" : "none";
    if (el("art").dataset.src !== s.image) { el("art").src = s.image; el("art").dataset.src = s.image; }
    el("title").textContent = s.title || "";
    el("artist").textContent = s.artist || "";
    el("album").textContent = s.album || "";
    if (s.duration) {
      el("fill").style.width = Math.min(100 * s.position / s.duration, 100) + "%";
      el("elapsed").textContent = fmt(s.position);
      el("total").textContent = fmt(s.duration);
      el("times").style.display = "flex";
    } else {
      el("fill").style.width = "0%";
      el("times").style.display = "none";
    }
  } catch (e) {}
}
setInterval(tick, 1000);
tick();
</script>
</body>
</html>
"""


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


class PresenceConfig:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mapping: dict[str, str] = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if "large_text" not in data:
            log.info("config %s predates the fourth presence line, applying new defaults", self.path)
            return
        mapping = dict(DEFAULT_CONFIG)
        for slot in SLOTS:
            value = data.get(slot)
            if valid_source(value):
                mapping[slot] = value
        display = data.get("status_display")
        if display in STATUS_DISPLAY_OPTIONS:
            mapping["status_display"] = display
        with self._lock:
            self._mapping = mapping

    def get(self) -> dict[str, str]:
        with self._lock:
            return dict(self._mapping)

    def set(self, mapping: dict[str, str]) -> dict[str, str]:
        with self._lock:
            self._mapping = dict(mapping)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("cannot save config to %s: %s", self.path, exc)
        return self.get()


class ProgressState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._position = 0.0
        self._duration = 0.0
        self._at = 0.0
        self.ever = False

    def set(self, position: float, duration: float) -> None:
        with self._lock:
            self._position = position
            self._duration = duration
            self._at = time.monotonic()
            self.ever = True

    def stale(self, timeout: float) -> bool:
        with self._lock:
            return self.ever and (time.monotonic() - self._at) > timeout

    def snapshot(self, max_age: float | None = None) -> tuple[float, float] | None:
        with self._lock:
            if self._duration <= 0:
                return None
            age = time.monotonic() - self._at
            if max_age is not None and age > max_age:
                return None
            position = min(self._position + max(0.0, min(age, 3.0)), self._duration)
            return position, self._duration


class AppState:
    def __init__(self, progress: ProgressState):
        self._lock = threading.Lock()
        self._progress = progress
        self._meta: dict | None = None
        self._image: str | None = None
        self._paused = False

    def set_now_playing(self, meta: dict, image: str | None) -> None:
        with self._lock:
            self._meta = dict(meta)
            self._image = image
            self._paused = False

    def set_paused(self) -> None:
        with self._lock:
            self._paused = True

    def set_idle(self) -> None:
        with self._lock:
            self._meta = None
            self._image = None
            self._paused = False

    def snapshot(self) -> dict:
        with self._lock:
            meta = dict(self._meta) if self._meta else None
            image = self._image
            paused = self._paused
        if not meta:
            return {"playing": False, "paused": False}
        progress = self._progress.snapshot()
        return {
            "playing": not paused,
            "paused": paused,
            "title": meta.get("title"),
            "artist": meta.get("artist") or "",
            "album": meta.get("album") or "",
            "position": progress[0] if progress else None,
            "duration": progress[1] if progress else None,
            "image": image or "/art",
        }


class PresenceBridge:
    def __init__(self, client_id: str, min_interval: float, dry_run: bool):
        self.client_id = client_id
        self.min_interval = min_interval
        self.dry_run = dry_run
        self._rpc: Presence | None = None
        self._last_send = 0.0
        self._last_identity: dict | None = None
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

    def update(self, payload: dict, throttle: float | None = None) -> None:
        identity = {key: payload.get(key) for key in IDENTITY_KEYS}
        if not self._cleared and identity == self._last_identity:
            log.debug("payload unchanged, skipping")
            return
        was_cleared = self._cleared
        if self._send("update", payload, throttle if throttle else (RESUME_THROTTLE if was_cleared else self.min_interval)):
            self._last_identity = identity
            self._cleared = False

    def clear(self) -> None:
        if self._cleared:
            return
        if self._send("clear", None, RESUME_THROTTLE):
            self._last_identity = None
            self._cleared = True

    def close(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None

    def _throttle(self, min_wait: float) -> None:
        wait = self._last_send + min_wait - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _send(self, op: str, payload: dict | None, throttle: float) -> bool:
        if self.dry_run:
            log.info("dry-run %s: %s", op, payload)
            self._last_send = time.monotonic()
            return True
        for _ in range(4):
            self._throttle(throttle)
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
        self._last_identity = None
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


def build_payload(
    meta: dict,
    image_url: str | None,
    progress: tuple[float, float] | None,
    mapping: dict[str, str],
) -> dict:
    payload = {"activity_type": ActivityType.LISTENING}
    for slot in SLOTS:
        field = mapping.get(slot)
        if not field or field == "none":
            continue
        value = clip(APPLE_MUSIC_TEXT) if field == APPLE_MUSIC else clip(meta.get(field))
        if value:
            payload[slot] = value
    if image_url:
        payload["large_image"] = image_url
    display = mapping.get("status_display")
    if display not in payload:
        display = "name"
    payload["status_display_type"] = STATUS_DISPLAY_MAP[display]
    if progress and progress[1] > 0:
        position, duration = progress
        now = int(time.time())
        payload["start"] = now - int(position)
        if duration > position:
            payload["end"] = now + int(duration - position)
    return payload


def spawn_uxplay(args: argparse.Namespace, meta_path: Path, image_path: Path) -> subprocess.Popen:
    command = ["uxplay"]
    if args.port is None:
        command.append("-p")
    else:
        command += ["-p", str(args.port)]
    if args.name:
        command += ["-n", args.name]
    command += ["-vs", "0", "-md", str(meta_path), "-ca", str(image_path)]
    log.info("starting: %s", " ".join(command))
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )


def read_uxplay_output(
    proc: subprocess.Popen,
    progress: ProgressState,
    tail: collections.deque,
    dead: threading.Event,
) -> None:
    stream = proc.stdout
    while True:
        line = stream.readline()
        if not line:
            break
        text = line.rstrip()
        tail.append(text)
        log.debug("uxplay: %s", text)
        match = PROGRESS_RE.search(text)
        if match:
            position = int(match.group(1)) * 60 + int(match.group(2))
            remaining = int(match.group(3)) * 60 + int(match.group(4))
            duration = int(match.group(5)) * 60 + int(match.group(6))
            progress.set(float(position), float(duration))
    dead.set()


def wait_for_progress(
    progress: ProgressState, changed: bool, misses: list[int]
) -> tuple[float, float] | None:
    if not changed:
        return progress.snapshot()
    deadline = time.monotonic() + 3.0
    snap = None
    while snap is None and time.monotonic() < deadline:
        snap = progress.snapshot(max_age=8.0)
        if snap is None:
            time.sleep(0.2)
    if snap is None and not progress.ever:
        misses[0] += 1
        if misses[0] >= 3:
            log.info("no playback progress available, not waiting for it anymore")
    return snap


def run_worker(
    meta_path: Path,
    image_path: Path,
    bridge: PresenceBridge,
    artwork: ArtworkResolver,
    config: PresenceConfig,
    jobs: "queue.Queue[str]",
    stop_event: threading.Event,
    failed: threading.Event,
    state: AppState,
    progress: ProgressState,
    misses: list[int],
) -> None:
    if not bridge.connect():
        failed.set()
        stop_event.set()
        return

    def identity_of(meta: dict) -> tuple[str, str, str]:
        return (meta.get("title") or "", meta.get("artist") or "", meta.get("album") or "")

    def send_update(meta: dict, wait_progress: bool, throttle: float | None = None) -> None:
        nonlocal last_identity
        identity = identity_of(meta)
        changed = identity != last_identity
        last_identity = identity
        progress_snap = wait_for_progress(progress, changed, misses) if wait_progress else progress.snapshot()
        image_url = artwork.url_for(meta, image_path)
        state.set_now_playing(meta, image_url)
        payload = build_payload(meta, image_url, progress_snap, config.get())
        log.info(
            "now playing: %s | %s | %s%s",
            meta.get("title"),
            meta.get("artist", "?"),
            meta.get("album", "?"),
            f" | {progress_snap[0]:.0f}s of {progress_snap[1]:.0f}s" if progress_snap else "",
        )
        bridge.update(payload, throttle=throttle)

    def clear_presence(paused: bool) -> None:
        nonlocal last_identity
        bridge.clear()
        if paused:
            state.set_paused()
        else:
            state.set_idle()
        last_identity = None

    last_identity: tuple[str, str, str] | None = None
    active = False
    while not stop_event.is_set():
        try:
            job = jobs.get(timeout=0.5)
        except queue.Empty:
            job = None
        else:
            try:
                while True:
                    job = jobs.get_nowait()
            except queue.Empty:
                pass
        if job == "clear":
            log.info("metadata file gone or empty, clearing presence")
            clear_presence(paused=False)
            active = False
            continue
        if job in ("update", "refresh"):
            if not meta_path.exists():
                continue
            meta = parse_metadata(meta_path)
            if meta is None:
                continue
            if not meta.get("title"):
                log.info("no track in metadata, clearing presence")
                clear_presence(paused=False)
                active = False
                continue
            send_update(meta, wait_progress=(job == "update"), throttle=(RESUME_THROTTLE if job == "refresh" else None))
            active = True
            continue
        if active and progress.stale(PAUSE_TIMEOUT):
            log.info("playback paused or stream ended, clearing presence")
            clear_presence(paused=True)
            active = False
        elif not active and progress.snapshot(max_age=PAUSE_TIMEOUT) is not None:
            if meta_path.exists():
                meta = parse_metadata(meta_path)
                if meta and meta.get("title"):
                    log.info("playback resumed, restoring presence")
                    send_update(meta, wait_progress=False, throttle=RESUME_THROTTLE)
                    active = True
    bridge.close()


def make_ui_server(
    port: int,
    state: AppState,
    image_path: Path,
    config: PresenceConfig,
    on_config_change,
) -> ThreadingHTTPServer | None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            log.debug("ui: " + format, *args)

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif self.path == "/state":
                body = json.dumps(state.snapshot()).encode()
                self._send(200, "application/json", body)
            elif self.path == "/config":
                body = json.dumps(config.get()).encode()
                self._send(200, "application/json", body)
            elif self.path == "/art":
                try:
                    data = image_path.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                self._send(200, "image/jpeg", data)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/config":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
            except ValueError:
                self._send(400, "application/json", b'{"error": "invalid json"}')
                return
            if not isinstance(data, dict):
                self._send(400, "application/json", b'{"error": "invalid body"}')
                return
            mapping: dict[str, str] = {}
            for slot in SLOTS:
                value = data.get(slot)
                if not valid_source(value):
                    self._send(400, "application/json", b'{"error": "invalid fields"}')
                    return
                mapping[slot] = value
            display = data.get("status_display")
            if display is not None:
                if display not in STATUS_DISPLAY_OPTIONS:
                    self._send(400, "application/json", b'{"error": "invalid fields"}')
                    return
                mapping["status_display"] = display
            saved = config.set(mapping)
            on_config_change()
            self._send(200, "application/json", json.dumps(saved).encode())

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log.warning("web ui disabled: cannot bind port %d (%s)", port, exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("web ui at http://127.0.0.1:%d", port)
    return server


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
    parser.add_argument("--port", type=int, default=None, help="AirPlay TCP port for uxplay (default: uxplay's legacy port set)")
    parser.add_argument("--name", default=None, help="AirPlay server name shown on clients")
    parser.add_argument("--ui-port", type=int, default=8080, help="port for the local web UI (default 8080)")
    parser.add_argument("--no-ui", action="store_true", help="disable the local web UI")
    parser.add_argument("--no-browser", action="store_true", help="do not open the web UI in a browser on startup")
    parser.add_argument("--config", default=None, help="path for the presence-lines config (default ~/.config/uxplay-presence/config.json)")
    parser.add_argument("--no-uxplay", action="store_true", help="do not spawn uxplay; watch files written by an externally started uxplay")
    parser.add_argument("--debounce", type=float, default=0.5, help="seconds to wait after the last file event")
    parser.add_argument("--min-interval", type=float, default=15.0, help="minimum seconds between regular Discord updates")
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
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser() if args.config else Path.home() / ".config" / "uxplay-presence" / "config.json"

    progress = ProgressState()
    state = AppState(progress)
    config = PresenceConfig(config_path)
    bridge = PresenceBridge(args.client_id or "", args.min_interval, args.dry_run)
    artwork = ArtworkResolver(ImageUploader(not args.no_image))
    jobs: "queue.Queue[str]" = queue.Queue()
    stop_event = threading.Event()
    failed = threading.Event()
    crashed = threading.Event()
    misses = [0]
    debouncer = Debouncer(args.debounce, jobs.put)

    ui_server = None
    if not args.no_ui:
        ui_server = make_ui_server(args.ui_port, state, image_path, config, lambda: jobs.put("refresh"))

    proc: subprocess.Popen | None = None
    tail: collections.deque = collections.deque(maxlen=15)
    uxplay_dead = threading.Event()
    if not args.no_uxplay:
        proc = spawn_uxplay(args, meta_path, image_path)
        reader = threading.Thread(
            target=read_uxplay_output, args=(proc, progress, tail, uxplay_dead), daemon=True
        )
        reader.start()
        time.sleep(1.0)
        if proc.poll() is not None:
            log.error("uxplay exited immediately; recent output:\n%s", "\n".join(tail) or "(none)")
            return 1
        log.info("uxplay running (pid %d)", proc.pid)
        if ui_server is not None and not args.no_browser:
            url = f"http://127.0.0.1:{args.ui_port}"
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    worker = threading.Thread(
        target=run_worker,
        args=(meta_path, image_path, bridge, artwork, config, jobs, stop_event, failed, state, progress, misses),
        daemon=True,
    )
    worker.start()

    observer = Observer()
    observer.schedule(MetaFileHandler(meta_path.name, debouncer), str(meta_path.parent), recursive=False)
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
            if uxplay_dead.is_set() and proc is not None and proc.poll() is not None:
                log.error("uxplay terminated unexpectedly; recent output:\n%s", "\n".join(tail) or "(none)")
                crashed.set()
                stop_event.set()
    finally:
        debouncer.cancel()
        observer.stop()
        observer.join(timeout=5)
        stop_event.set()
        worker.join(timeout=5)
        if ui_server is not None:
            ui_server.shutdown()
            ui_server.server_close()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return 1 if (failed.is_set() or crashed.is_set()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
