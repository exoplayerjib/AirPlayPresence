from __future__ import annotations

import logging
import hashlib
import itertools
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .constants import (
    MIN_ART_BYTES, ART_SETTLE_TIMEOUT, UPLOAD_EXPIRY, UPLOAD_GATE_EXTENDED_SECONDS,
    UPLOAD_GATE_EXTENDED_STRIKES, UPLOAD_HTTP_FAILURE_BODY_CAPTURE, UPLOAD_HTTP_RETRIES,
    UPLOAD_HTTP_RETRY_BACKOFF, UPLOAD_RETRY, UPLOAD_TTL, UPLOAD_URL, UPLOAD_USER_AGENT,
    UPLOAD_VERIFY_TIMEOUT,
)


log = logging.getLogger("airplay-presence")

def valid_art_bytes(data: bytes) -> bool:
    return len(data) >= MIN_ART_BYTES and data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-4:]

_image_nonce = itertools.count(1)


def fresh_image_url(url: str) -> str:
    return f"{url}?v={next(_image_nonce)}"


def read_valid_art(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if valid_art_bytes(data) else None


def wait_valid_art(path: Path, timeout: float = ART_SETTLE_TIMEOUT) -> bytes | None:
    deadline = time.monotonic() + timeout
    while True:
        data = read_valid_art(path)
        if data is not None:
            return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


class ImageUploader:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._cache: dict[str, tuple[str, float]] = {}
        # why the last url_for attempt failed: "no-file" (invalid/missing art
        # file — retryable immediately) or "network" (litterbox refused —
        # server-side, needs a cooldown)
        self.last_failure_kind: str | None = None
        self.last_http_status: int | None = None

    def url_for(self, path: Path, wait: bool = True) -> str | None:
        if not self.enabled:
            self.last_failure_kind = "disabled"
            return None
        data = wait_valid_art(path) if wait else read_valid_art(path)
        if data is None:
            if wait:
                try:
                    raw = path.read_bytes()
                    if raw:
                        kind = "placeholder" if len(raw) < 200 else "wrong format"
                        log.warning(
                            "no valid cover art in %s (%d bytes, %s); retrying at the next heartbeat",
                            path, len(raw), kind,
                        )
                    else:
                        log.warning("cover art file %s is empty; retrying at the next heartbeat", path)
                except OSError:
                    log.warning("cover art file %s absent; retrying at the next heartbeat", path)
            self.last_failure_kind = "no-file"
            return None
        self.last_failure_kind = None
        self.last_http_status = None
        digest = hashlib.sha256(data).hexdigest()
        cached = self._cache.get(digest)
        if cached and time.monotonic() - cached[1] < UPLOAD_TTL:
            return cached[0]
        url = self._upload(data)
        if url:
            self.last_failure_kind = None
            self.last_http_status = None
            self._cache[digest] = (url, time.monotonic())
        return url

    def cache_info(self) -> list[tuple[str, float]]:
        now = time.monotonic()
        # snapshot the dict: the worker thread mutates the cache while the TUI iterates
        return [(d[:12], now - t) for d, (_, t) in list(self._cache.items())]

    def _upload(self, data: bytes) -> str | None:
        boundary = uuid.uuid4().hex
        body = b"".join((
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="reqtype"\r\n\r\n',
            b"fileupload",
            f"\r\n--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="time"\r\n\r\n{UPLOAD_EXPIRY}'.encode(),
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="fileToUpload"; filename="cover.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ))
        attempts = UPLOAD_HTTP_RETRIES + 1
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                UPLOAD_URL,
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": UPLOAD_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://litterbox.catbox.moe/",
                    "Origin": "https://litterbox.catbox.moe",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    url = response.read().decode("utf-8", "replace").strip()
            except urllib.error.HTTPError as exc:
                try:
                    err_body = exc.read(UPLOAD_HTTP_FAILURE_BODY_CAPTURE).decode("utf-8", "replace").strip()
                except Exception:
                    err_body = ""
                if exc.code < 500 or attempt >= attempts:
                    self.last_http_status = exc.code
                    self.last_failure_kind = "network"
                    log.warning(
                        "cover art upload rejected: HTTP %s from litterbox%s",
                        exc.code, (f"; body: {err_body!r}" if err_body else " (no body)"),
                    )
                    return None
                # transient server-side flake (litterbox 500s on valid uploads):
                # back off briefly and retry, 4xx still fails fast
                self._retry_pause(attempt, f"HTTP {exc.code}")
                continue
            except (urllib.error.URLError, OSError) as exc:
                if attempt >= attempts:
                    log.warning("cover art upload failed: %s", exc)
                    self.last_failure_kind = "network"
                    return None
                self._retry_pause(attempt, str(exc))
                continue
            if not url.startswith("https://"):
                log.warning("unexpected upload response: %r", url)
                self.last_failure_kind = "network"
                return None
            if not self._verify(url):
                log.warning("uploaded cover art not reachable yet: %s", url)
                self.last_failure_kind = "network"
                return None
            log.info("uploaded cover art: %s", url)
            return url
        return None

    def _retry_pause(self, attempt: int, cause: str) -> None:
        delay = UPLOAD_HTTP_RETRY_BACKOFF * attempt
        log.info(
            "litterbox upload failed (%s), retrying (%d/%d) in %gs",
            cause, attempt, UPLOAD_HTTP_RETRIES, delay,
        )
        time.sleep(delay)

    def _verify(self, url: str) -> bool:
        deadline = time.monotonic() + UPLOAD_VERIFY_TIMEOUT
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": UPLOAD_USER_AGENT}
        )
        while True:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

def norm_text(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value.lower()) if ch.isalnum()
    )


class ArtworkResolver:
    """Resolves cover art per track, preferring iTunes (optional) then uploads.

    Failure kinds are handled differently on purpose:
    - "no-file": the -ca file is absent/empty/placeholder — no network abuse,
      so the next heartbeat lookup may retry immediately (no gate armed).
    - "network": litterbox (or iTunes) actively failed — arms the
      UPLOAD_RETRY (60s) cooldown so heartbeats don't hammer a
      rate-limiting endpoint.
    """

    def __init__(self, uploader: ImageUploader, itunes: bool = False):
        self.uploader = uploader
        self.itunes = itunes
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self.last_failure_kind: str | None = None
        self._last_fail = 0.0
        self._gate_seconds = UPLOAD_RETRY
        self._network_strikes = 0

    def url_for(self, meta: dict, image_path: Path, wait: bool = True) -> str | None:
        title = meta.get("title") or ""
        artist = meta.get("artist") or ""
        key = (title, artist)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[1] < UPLOAD_TTL:
            self.last_failure_kind = None
            return cached[0]
        if now - self._last_fail < self._gate_seconds:
            # network cooldown: a still-valid cached link may be served, but
            # no new upload attempts until the cooldown expires
            return cached[0] if cached and now - cached[1] < UPLOAD_TTL else None
        url = self._lookup_itunes(title, artist) if self.itunes else None
        if not url:
            url = self.uploader.url_for(image_path, wait=wait)
        if url:
            self._cache[key] = (url, time.monotonic())
            self.last_failure_kind = None
            self.last_http_status = None
            self._network_strikes = 0
            self._gate_seconds = UPLOAD_RETRY
            return url
        kind = self.uploader.last_failure_kind
        self.last_failure_kind = kind
        # mirror the HTTP status for the TUI gate line
        self.last_http_status = self.uploader.last_http_status
        if kind == "network":
            # genuine server-side failure: arm the network cooldown;
            # placeholder-only states retry at the next heartbeat
            self._network_strikes += 1
            if self._network_strikes >= UPLOAD_GATE_EXTENDED_STRIKES and self._gate_seconds < UPLOAD_GATE_EXTENDED_SECONDS:
                self._gate_seconds = UPLOAD_GATE_EXTENDED_SECONDS
                log.warning(
                    "cover art upload failing repeatedly (%d strikes); cooldown extended to %ds",
                    self._network_strikes, int(UPLOAD_GATE_EXTENDED_SECONDS),
                )
            self._last_fail = now
        return cached[0] if cached and now - cached[1] < UPLOAD_TTL else None

    def cache_info(self) -> list[tuple[str, float]]:
        now = time.monotonic()
        return [(f"{k[0][:20]}|{k[1][:20]}", now - t) for k, (_, t) in list(self._cache.items())]

    def retry_gate_remaining(self) -> float:
        wait = self._gate_seconds - (time.monotonic() - self._last_fail)
        if self._last_fail <= 0.0:
            return 0.0
        return max(0.0, wait)

    def _lookup_itunes(self, title: str, artist: str) -> str | None:
        if not title:
            return None
        term = f"{title} {artist}".strip()
        params = urllib.parse.urlencode({"term": term, "entity": "song", "limit": "5"})
        request = urllib.request.Request(
            f"https://itunes.apple.com/search?{params}",
            headers={"User-Agent": "airplay-presence/1.0"},
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
        log.info("no iTunes match for %r, falling back to litterbox upload", title)
        return None
