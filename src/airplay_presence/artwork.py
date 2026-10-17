from __future__ import annotations

import base64
import logging
import hashlib
import itertools
import json
import queue
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .constants import (
    MIN_ART_BYTES, ART_SETTLE_TIMEOUT, SPOTIFY_TOKEN_MARGIN, UPLOAD_EXPIRY,
    UPLOAD_GATE_EXTENDED_SECONDS,
    UPLOAD_GATE_EXTENDED_STRIKES, UPLOAD_HTTP_FAILURE_BODY_CAPTURE, UPLOAD_HTTP_RETRIES,
    UPLOAD_HTTP_RETRY_BACKOFF, UPLOAD_RETRY, UPLOAD_RETRY_TRANSIENT, UPLOAD_TTL,
    UPLOAD_URL, UPLOAD_USER_AGENT, UPLOAD_VERIFY_TIMEOUT,
)


log = logging.getLogger("airplay-presence")

def valid_art_bytes(data: bytes) -> bool:
    return len(data) >= MIN_ART_BYTES and data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-4:]

_image_nonce = itertools.count(1)


def fresh_image_url(url: str) -> str:
    return f"{url}?v={next(_image_nonce)}"


def read_valid_art(path: Path, accept=None) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not valid_art_bytes(data):
        return None
    return data if accept is None or accept(data) else None


def wait_valid_art(path: Path, timeout: float = ART_SETTLE_TIMEOUT, accept=None) -> bytes | None:
    deadline = time.monotonic() + timeout
    while True:
        data = read_valid_art(path, accept)
        if data is not None:
            return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


class ImageUploader:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._cache: dict[str, tuple[str, float]] = {}
        # digest -> album (or title) of the track the art was first accepted for
        self._owners: dict[str, str] = {}
        self._lock = threading.Lock()
        # why the last url_for attempt failed: "no-file" (invalid/missing art
        # file — retryable immediately) or "network" (litterbox refused —
        # server-side, needs a cooldown)
        self.last_failure_kind: str | None = None
        self.last_http_status: int | None = None

    def _accepts(self, data: bytes, owner: str) -> bool:
        with self._lock:
            known = self._owners.get(hashlib.sha256(data).hexdigest())
        return known is None or known == owner

    def url_for(self, path: Path, wait: bool = True, owner: str | None = None) -> str | None:
        if not self.enabled:
            self.last_failure_kind = "disabled"
            return None
        accept = None if owner is None else (lambda data: self._accepts(data, owner))
        data = wait_valid_art(path, accept=accept) if wait else read_valid_art(path, accept=accept)
        if data is None:
            if wait:
                try:
                    raw = path.read_bytes()
                    if not raw:
                        log.warning("cover art file %s is empty; retrying at the next heartbeat", path)
                    elif valid_art_bytes(raw):
                        log.info("cover art in %s is still the previous track's; retrying at the next heartbeat", path)
                    else:
                        kind = "placeholder" if len(raw) < 200 else "wrong format"
                        log.warning(
                            "no valid cover art in %s (%d bytes, %s); retrying at the next heartbeat",
                            path, len(raw), kind,
                        )
                except OSError:
                    log.warning("cover art file %s absent; retrying at the next heartbeat", path)
            self.last_failure_kind = "no-file"
            return None
        self.last_failure_kind = None
        self.last_http_status = None
        digest = hashlib.sha256(data).hexdigest()
        if owner is not None:
            with self._lock:
                self._owners.setdefault(digest, owner)
        with self._lock:
            cached = self._cache.get(digest)
        if cached and time.monotonic() - cached[1] < UPLOAD_TTL:
            return cached[0]
        url = self._upload(data)
        if url:
            self.last_failure_kind = None
            self.last_http_status = None
            with self._lock:
                self._cache[digest] = (url, time.monotonic())
        return url

    def cache_info(self) -> list[tuple[str, float]]:
        now = time.monotonic()
        # snapshot the dict under the lock: the art thread mutates the cache
        # (uploads) while the TUI iterates it
        with self._lock:
            return [(d[:12], now - t) for d, (_, t) in self._cache.items()]

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


class SpotifyLookup:
    """Fallback art source: Spotify Web API via the client-credentials flow.

    Needs a free Spotify application's client id + secret (no scopes —
    public catalog search only). The access token is cached until
    SPOTIFY_TOKEN_MARGIN before it expires; the track match reuses the
    iTunes rules (normalized title equality + artist-token overlap) and
    returns the largest album image (640px, i.scdn.co — public CDN)."""

    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()

    def _access_token(self) -> str | None:
        with self._lock:
            if self._token and time.monotonic() < self._token_exp:
                return self._token
        credentials = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        request = urllib.request.Request(
            self.TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "airplay-presence/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("spotify token request failed: %s", exc)
            return None
        token = data.get("access_token")
        if not token:
            log.warning("spotify token response had no access_token: %r", data)
            return None
        expires = max(0.0, float(data.get("expires_in") or 3600) - SPOTIFY_TOKEN_MARGIN)
        with self._lock:
            self._token = token
            self._token_exp = time.monotonic() + expires
        return token

    def lookup(self, title: str, artist: str) -> str | None:
        if not title:
            return None
        token = self._access_token()
        if not token:
            return None
        params = urllib.parse.urlencode(
            {"type": "track", "limit": "5", "q": f'track:"{title}" artist:"{artist}"'}
        )
        request = urllib.request.Request(
            f"{self.SEARCH_URL}?{params}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "airplay-presence/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("spotify search failed: %s", exc)
            return None
        url = self._pick(data, title, artist)
        if url:
            log.info("artwork from Spotify: %s", url)
        else:
            log.info("no Spotify match for %r, falling back to litterbox upload", title)
        return url

    @staticmethod
    def _pick(data: dict, title: str, artist: str) -> str | None:
        want_title = norm_text(title)
        artist_tokens = {t for t in re.split(r"\W+", artist.lower()) if len(t) >= 3}
        for item in data.get("tracks", {}).get("items", []):
            if norm_text(item.get("name", "")) != want_title:
                continue
            names = " ".join(a.get("name", "") for a in item.get("artists", [])).lower()
            if artist_tokens and not any(tok in names for tok in artist_tokens):
                continue
            images = item.get("album", {}).get("images") or []
            if not images:
                continue
            best = max(images, key=lambda im: im.get("width") or 0)
            if best.get("url"):
                return best["url"]
        return None


class ArtworkResolver:
    """Resolves cover art per track, preferring iTunes (optional) then uploads.

    Failure kinds are handled differently on purpose:
    - "no-file": the -ca file is absent/empty/placeholder — no network abuse,
      so the next heartbeat lookup may retry immediately (no gate armed).
    - "network": litterbox (or iTunes) actively failed — arms a cooldown
      split by failure class: 4xx refusals (412 rate-limit / 403 forbidden)
      get the full UPLOAD_RETRY (60s); 5xx flakes and connection errors
      get UPLOAD_RETRY_TRANSIENT (20s). Reaching 3 strikes extends either
      class to the 300s extended cooldown.

    Resolution chain per track: iTunes Search (if enabled) → Spotify Web
    API (if credentials are configured) → litterbox upload. The first
    source that returns a URL wins; misses move down the chain.
    """

    def __init__(self, uploader: ImageUploader, itunes: bool = False, spotify: "SpotifyLookup | None" = None):
        self.uploader = uploader
        self.itunes = itunes
        self.spotify = spotify
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self.last_failure_kind: str | None = None
        self._last_fail = 0.0
        self._gate_seconds = UPLOAD_RETRY
        self._network_strikes = 0

    def cached_url(self, meta: dict) -> str | None:
        """Cache-only lookup for the worker loop: no file reads, no network,
        no gate arming — safe to call any time; the art thread owns the
        blocking resolution."""
        key = (meta.get("title") or "", meta.get("artist") or "")
        with self._lock:
            cached = self._cache.get(key)
        if cached and time.monotonic() - cached[1] < UPLOAD_TTL:
            return cached[0]
        return None

    def url_for(self, meta: dict, image_path: Path, wait: bool = True) -> str | None:
        title = meta.get("title") or ""
        artist = meta.get("artist") or ""
        key = (title, artist)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
        if cached and now - cached[1] < UPLOAD_TTL:
            self.last_failure_kind = None
            return cached[0]
        if now - self._last_fail < self._gate_seconds:
            # network cooldown: a still-valid cached link may be served, but
            # no new upload attempts until the cooldown expires
            return cached[0] if cached and now - cached[1] < UPLOAD_TTL else None
        url = self._lookup_itunes(title, artist) if self.itunes else None
        if not url and self.spotify is not None:
            url = self.spotify.lookup(title, artist)
        if not url:
            # uxplay keeps the previous track's cover in the -ca file until the
            # new one arrives; a cover first accepted for another album is
            # stale and must not be uploaded under this track
            owner = (meta.get("album") or title).strip().lower()
            url = self.uploader.url_for(image_path, wait=wait, owner=owner)
        if url:
            with self._lock:
                self._cache[key] = (url, time.monotonic())
            self.last_failure_kind = None
            self.last_http_status = None
            self._network_strikes = 0
            self._gate_seconds = UPLOAD_RETRY
            return url
        kind = self.uploader.last_failure_kind
        self.last_failure_kind = kind
        self.last_http_status = self.uploader.last_http_status if kind == "network" else None
        if kind == "network":
            # genuine server-side failure: arm the network cooldown;
            # placeholder-only states retry at the next heartbeat.
            # 4xx refusals (rate-limited/forbidden) keep the long backoff —
            # hammering them prolongs the block; 5xx flakes and connection
            # errors are transient and retry sooner
            self._network_strikes += 1
            status = self.uploader.last_http_status
            refusal = status is not None and status < 500
            self._gate_seconds = UPLOAD_RETRY if refusal else UPLOAD_RETRY_TRANSIENT
            if self._network_strikes >= UPLOAD_GATE_EXTENDED_STRIKES and self._gate_seconds < UPLOAD_GATE_EXTENDED_SECONDS:
                self._gate_seconds = UPLOAD_GATE_EXTENDED_SECONDS
                log.warning(
                    "cover art upload failing repeatedly (%d strikes); cooldown extended to %ds",
                    self._network_strikes, int(UPLOAD_GATE_EXTENDED_SECONDS),
                )
            self._last_fail = now
        with self._lock:
            cached = self._cache.get(key)
        return cached[0] if cached and now - cached[1] < UPLOAD_TTL else None

    def cache_info(self) -> list[tuple[str, float]]:
        now = time.monotonic()
        with self._lock:
            return [(f"{k[0][:20]}|{k[1][:20]}", now - t) for k, (_, t) in self._cache.items()]

    def retry_gate_remaining(self) -> float:
        with self._lock:
            if self._last_fail <= 0.0:
                return 0.0
            wait = self._gate_seconds - (time.monotonic() - self._last_fail)
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


class ArtWorker:
    """Dedicated thread for all blocking artwork work: art-settle polling,
    uploads, verification, gate backoff.

    The worker loop stays free: it submits requests via request() (a
    cache-only lookup decides whether one is needed) and receives results
    as ("art", (title, artist), url) jobs in its queue. Result application —
    MPRIS republish, payload rebuild, Discord send — stays on the worker
    thread; only the network waits live here."""

    def __init__(
        self,
        resolver: ArtworkResolver,
        image_path: Path,
        submit,
        stop_event: threading.Event,
    ) -> None:
        self._resolver = resolver
        self._image_path = image_path
        self._submit = submit
        self._stop_event = stop_event
        self._requests: "queue.Queue[dict]" = queue.Queue()
        self._thread: threading.Thread | None = None

    def request(self, meta: dict) -> None:
        self._requests.put(dict(meta))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="artwork", daemon=True)
        self._thread.start()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                meta = self._requests.get(timeout=0.5)
            except queue.Empty:
                continue
            # drain to the newest request: only the latest track matters
            while True:
                try:
                    meta = self._requests.get_nowait()
                except queue.Empty:
                    break
            try:
                url = self._resolver.url_for(meta, self._image_path, wait=True)
            except Exception:
                log.exception("artwork resolution failed")
                url = None
            self._submit(("art", (meta.get("title") or "", meta.get("artist") or ""), url))
