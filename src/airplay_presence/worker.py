from __future__ import annotations

import logging
import queue
import threading
import time

from watchdog.events import FileSystemEventHandler

from .artwork import ArtworkResolver, fresh_image_url
from .config import PresenceConfig
from .constants import (
    HEARTBEAT_QUIET, PAUSE_TIMEOUT, RESUME_THROTTLE, SEEK_BASE_MAX_AGE,
    STATUS_DISPLAY_MAP,
)
from .mpris import MprisServer
from .presence import PresenceBridge, build_payload
from .state import AppState, ProgressState
from .utils import parse_metadata
from .uxplay import new_track_signature


log = logging.getLogger("airplay-presence")

class Debouncer:
    def __init__(self, delay: float, submit):
        self.delay = delay
        self.submit = submit
        self.last_schedule = 0.0
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def schedule(self, job: str) -> None:
        with self._lock:
            self.last_schedule = time.monotonic()
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

def wait_for_progress(
    progress: ProgressState,
    changed_at: float | None,
    prior: tuple[float, float] | None,
    misses: list[int],
) -> tuple[float, float] | None:
    if changed_at is None:
        return progress.snapshot()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        cur = progress.snapshot(min_at=changed_at, raw=True)
        if cur is not None and new_track_signature(cur, prior):
            return progress.snapshot(min_at=changed_at)
        time.sleep(0.2)
    if prior is None:
        misses[0] += 1
        if misses[0] >= 3:
            log.info("no playback progress available, not waiting for it anymore")
    return None

def run_worker(
    meta_path: Path,
    image_path: Path,
    bridge: PresenceBridge,
    artwork: ArtworkResolver,
    config: PresenceConfig,
    jobs: "queue.Queue[str]",
    stop_event: threading.Event,
    state: AppState,
    progress: ProgressState,
    misses: list[int],
    mpris: "MprisServer | None",
    debouncer: Debouncer,
) -> None:
    def identity_of(meta: dict) -> tuple[str, str, str]:
        return (meta.get("title") or "", meta.get("artist") or "", meta.get("album") or "")

    def send_update(meta: dict, wait_progress: bool, throttle: float | None = None, force: bool = False) -> None:
        nonlocal last_identity, last_seen_position, timer_pending, pending_prior, current_meta, current_image, last_payload
        last_seen_position = None
        identity = identity_of(meta)
        changed = identity != last_identity
        last_identity = identity
        if wait_progress:
            prior = progress.seek_base(SEEK_BASE_MAX_AGE) or progress.snapshot(raw=True)
            changed_at = time.monotonic() if changed else None
            progress_snap = wait_for_progress(progress, changed_at, prior, misses)
            timer_pending = progress_snap is None
            pending_prior = prior if timer_pending else None
        else:
            progress_snap = progress.snapshot()
            timer_pending = progress_snap is None
            pending_prior = None
        image_url = artwork.url_for(meta, image_path)
        state.set_now_playing(meta, image_url)
        current_meta = meta
        current_image = image_url
        if mpris is not None:
            mpris.publish(meta, image_url, paused=False)
        mapping = config.get()
        payload = build_payload(
            meta,
            fresh_image_url(image_url) if image_url else None,
            progress_snap,
            mapping,
        )
        line = status_line_for(mapping)
        if mapping.get("status_rotation"):
            payload["status_display_type"] = STATUS_DISPLAY_MAP[line]
        state.set_status_line(line)
        log.info(
            "now playing: %s | %s | %s%s",
            meta.get("title"),
            meta.get("artist", "?"),
            meta.get("album", "?"),
            f" | {progress_snap[0]:.0f}s of {progress_snap[1]:.0f}s" if progress_snap else "",
        )
        bridge.update(payload, throttle=throttle, force=force)
        last_payload = payload
        state.set_payload(payload)

    def clear_presence(paused: bool) -> None:
        nonlocal last_identity, current_meta, current_image
        bridge.clear()
        state.set_payload(None)
        if paused:
            state.set_paused()
            if mpris is not None:
                mpris.publish(current_meta, current_image, paused=True)
        else:
            state.set_idle()
            current_meta = None
            current_image = None
            if mpris is not None:
                mpris.publish(None, None, paused=False)
        last_identity = None

    last_identity: tuple[str, str, str] | None = None
    last_seen_position: float | None = None
    last_seen_seek = 0
    timer_pending = False
    pending_prior: tuple[float, float] | None = None
    current_meta: dict | None = None
    current_image: str | None = None
    active = False
    last_payload: dict | None = None
    last_job_at = 0.0
    rot_idx = 0
    last_rotation = time.monotonic()

    def status_line_for(mapping: dict) -> str:
        order = mapping.get("status_rotation_order") or []
        if mapping.get("status_rotation") and order:
            return order[rot_idx % len(order)]
        return mapping.get("status_display") or "state"
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
            last_job_at = time.monotonic()
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
            last_seen_seek = progress.seek_seq
            active = True
            continue
        if active and progress.stale(PAUSE_TIMEOUT):
            log.info("playback paused or stream ended, clearing presence")
            clear_presence(paused=True)
            active = False
        elif not active:
            snap = progress.snapshot(max_age=PAUSE_TIMEOUT, raw=True)
            if snap is not None:
                if last_seen_position is not None and abs(snap[0] - last_seen_position) >= 0.5:
                    if meta_path.exists():
                        meta = parse_metadata(meta_path)
                        if meta and meta.get("title"):
                            log.info("playback resumed, restoring presence")
                            send_update(meta, wait_progress=False, throttle=RESUME_THROTTLE)
                            active = True
                else:
                    last_seen_position = snap[0]
        elif progress.seek_seq != last_seen_seek and debouncer.last_schedule <= last_job_at:
            last_seen_seek = progress.seek_seq
            if current_meta is not None and meta_path.exists():
                log.info("track seek detected, updating presence timer")
                send_update(current_meta, wait_progress=False, throttle=RESUME_THROTTLE, force=True)
        elif (
            last_payload is not None
            and bridge.idle_for() >= bridge.min_interval
            and time.monotonic() - debouncer.last_schedule >= HEARTBEAT_QUIET
        ):
            payload = dict(last_payload)
            if current_meta is not None:
                image_url = artwork.url_for(current_meta, image_path, wait=False)
                if image_url != current_image:
                    current_image = image_url
                    if mpris is not None:
                        mpris.publish(current_meta, image_url, paused=False)
                if image_url:
                    payload["large_image"] = fresh_image_url(image_url)
                else:
                    payload.pop("large_image", None)
            mapping = config.get()
            if mapping.get("status_rotation"):
                order = mapping.get("status_rotation_order") or []
                nowm = time.monotonic()
                if len(order) > 1 and nowm - last_rotation >= mapping.get(
                    "status_rotation_interval", 30
                ):
                    rot_idx = (rot_idx + 1) % len(order)
                    last_rotation = nowm
                    log.info("member-list line rotated to %s", order[rot_idx])
                line = status_line_for(mapping)
                payload["status_display_type"] = STATUS_DISPLAY_MAP[line]
                state.set_status_line(line)
            bridge.update(payload, force=True)
        elif timer_pending:
            snap = progress.snapshot(max_age=PAUSE_TIMEOUT, raw=True)
            if (
                snap is not None
                and snap != pending_prior
                and new_track_signature(snap, pending_prior)
                and meta_path.exists()
            ):
                meta = parse_metadata(meta_path)
                if meta and meta.get("title"):
                    log.info("track progress arrived, refreshing presence timer")
                    send_update(meta, wait_progress=False, throttle=RESUME_THROTTLE)
    bridge.close()
