from __future__ import annotations

import logging
import queue
import threading
import time

from pypresence import ActivityType
from pypresence.exceptions import (
    InvalidID, PipeClosed, PyPresenceException, ResponseTimeout, ServerError,
)

from .constants import (
    APPLE_MUSIC, APPLE_MUSIC_TEXT, CUSTOM_PREFIX, IDENTITY_KEYS,
    RESUME_THROTTLE, SLOTS, STATUS_DISPLAY_MAP,
)
from .utils import clean_value, clip


log = logging.getLogger("airplay-presence")

class PresenceBridge:
    def __init__(self, client_id: str, min_interval: float, dry_run: bool):
        self.client_id = client_id
        self.min_interval = min_interval
        self.dry_run = dry_run
        self._rpc: Presence | None = None
        self._last_send = 0.0
        self._last_identity: dict | None = None
        self._cleared = True
        self._queue: "queue.Queue[tuple[str, dict | None, float | None, bool] | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._failed = threading.Event()
        self._stop_event = threading.Event()

    def start(self, failed: threading.Event, stop_event: threading.Event) -> None:
        self._failed = failed
        self._stop_event = stop_event
        self._thread = threading.Thread(target=self._run, name="discord", daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self.dry_run or self._rpc is not None

    @property
    def last_send_age(self) -> "float | None":
        if self._last_send <= 0.0:
            return None
        return time.monotonic() - self._last_send

    def update(self, payload: dict, throttle: float | None = None, force: bool = False) -> None:
        self._queue.put(("update", payload, throttle, force))

    def clear(self) -> None:
        self._queue.put(("clear", None, RESUME_THROTTLE, False))

    def close(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        if not self._connect():
            self._failed.set()
            self._stop_event.set()
            return
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            self._process(*item)
        self._disconnect()

    def _connect(self) -> bool:
        if self.dry_run:
            log.info("dry run: skipping Discord connection")
            return True
        backoff = 5.0
        while not self._stop_event.is_set():
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
                if self._stop_event.wait(backoff):
                    return False
                backoff = min(backoff * 2, 60)
                continue
            self._rpc = rpc
            self._last_send = 0.0
            log.info("connected to Discord IPC")
            return True
        return False

    def _process(self, op: str, payload: dict | None, hint: float | None, force: bool) -> None:
        if op == "update":
            identity = {key: payload.get(key) for key in IDENTITY_KEYS}
            identity["has_timer"] = bool(payload.get("start"))
            if not force and not self._cleared and identity == self._last_identity:
                log.info("discord: skipped (payload unchanged)")
                return
            throttle = hint or (RESUME_THROTTLE if self._cleared else self.min_interval)
            self._throttle(throttle)
        else:
            if self._cleared:
                return
            self._throttle(RESUME_THROTTLE)
        for op, payload, hint, force in self._absorb(op, payload, hint, force):
            self._dispatch(op, payload, force)

    def _absorb(self, op: str, payload: dict | None, hint: float | None, force: bool) -> "list[tuple[str, dict | None, float | None, bool]]":
        items: "list[tuple[str, dict | None, float | None, bool]]" = [(op, payload, hint, force)]
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                self._queue.put(nxt)
                break
            items.append(nxt)
        out: "list[tuple[str, dict | None, float | None, bool]]" = []
        for it in items:
            if out and out[-1][0] == "update" and it[0] in ("update", "clear"):
                out[-1] = it
            else:
                out.append(it)
        if len(out) < len(items):
            log.info("discord: coalesced %d queued updates", len(items) - len(out))
        return out

    def _dispatch(self, op: str, payload: dict | None, force: bool) -> None:
        if op == "update":
            identity = {key: payload.get(key) for key in IDENTITY_KEYS}
            identity["has_timer"] = bool(payload.get("start"))
            if not force and not self._cleared and identity == self._last_identity:
                log.info("discord: skipped (payload unchanged)")
                return
            if self._send("update", payload):
                self._last_identity = identity
                self._cleared = False
                log.info("discord: presence updated")
        else:
            if self._cleared:
                return
            if self._send("clear", None):
                self._last_identity = None
                self._cleared = True
                log.info("discord: presence cleared")

    def idle_for(self) -> float:
        return time.monotonic() - self._last_send

    def _throttle(self, min_wait: float) -> None:
        wait = self._last_send + min_wait - time.monotonic()
        if wait > 0.5:
            log.info("discord: rate limit, sending in %.0fs", wait)
        if wait > 0:
            time.sleep(wait)

    def _send(self, op: str, payload: dict | None) -> bool:
        for _ in range(4):
            if self.dry_run:
                log.info("dry-run %s: %s", op, payload)
                self._last_send = time.monotonic()
                return True
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
        self._disconnect()
        self._last_identity = None
        self._cleared = True
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                rpc = Presence(self.client_id)
                rpc.connect()
            except PyPresenceException as exc:
                log.warning("reconnect failed (%s), retrying in %.0fs", exc, backoff)
                if self._stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, 60)
                continue
            self._rpc = rpc
            log.info("reconnected to Discord IPC")
            return

    def _disconnect(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None

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
        if field == APPLE_MUSIC:
            value = clip(APPLE_MUSIC_TEXT)
        elif field.startswith(CUSTOM_PREFIX):
            value = clip(clean_value(field[len(CUSTOM_PREFIX):]))
        else:
            value = clip(meta.get(field))
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
