from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler

from .artwork import ArtworkResolver, ArtWorker, fresh_image_url
from .config import PresenceConfig
from .constants import (
    HEARTBEAT_QUIET, PAUSE_TIMEOUT, PROGRESS_WAIT, RESUME_THROTTLE, SEEK_BASE_MAX_AGE,
    STATUS_DISPLAY_MAP,
)
from .mpris import MprisServer
from .presence import PresenceBridge, build_payload, slot_value
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
) -> tuple[float, float] | None:
    if changed_at is None:
        return progress.snapshot()
    # short grace wait: the new track's first progress print usually lands
    # within ~1s of the metadata event (the \r-trap holds the old track's
    # final print until the next stdout byte). If it hasn't arrived by the
    # deadline, publish timer-less — the idle tick's timer_pending branch
    # attaches the timer when it lands; the worker never blocks longer.
    deadline = time.monotonic() + PROGRESS_WAIT
    while time.monotonic() < deadline:
        cur = progress.snapshot(min_at=changed_at, raw=True)
        if cur is not None and new_track_signature(cur, prior):
            return progress.snapshot(min_at=changed_at)
        time.sleep(0.2)
    return None

def run_worker(
    meta_path: Path,
    bridge: PresenceBridge,
    artwork: ArtworkResolver,
    config: PresenceConfig,
    jobs: "queue.Queue[str | tuple]",
    stop_event: threading.Event,
    state: AppState,
    progress: ProgressState,
    mpris: "MprisServer | None",
    debouncer: Debouncer,
    art_worker: ArtWorker,
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
            progress_snap = wait_for_progress(progress, changed_at, prior)
            timer_pending = progress_snap is None
            pending_prior = prior if timer_pending else None
        else:
            progress_snap = progress.snapshot()
            timer_pending = progress_snap is None
            pending_prior = None
        # art never blocks the publish: serve a cached link if we have one,
        # otherwise send without art and let the art thread resolve async —
        # its result arrives as an ("art", key, url) job
        image_url = artwork.cached_url(meta)
        if image_url is None:
            art_worker.request(meta)
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

    def apply_art(key: tuple[str, str], url: str | None) -> None:
        """Attach an asynchronously resolved art URL to the current track.

        Runs on the worker thread as a job: all presence/MPRIS state stays
        single-threaded; the art thread only did the network waiting."""
        nonlocal current_image, last_payload
        if not active or current_meta is None:
            return
        if (current_meta.get("title") or "", current_meta.get("artist") or "") != key:
            return  # stale result for a track that is no longer current
        if not url or url == current_image:
            return
        current_image = url
        state.set_now_playing(current_meta, url)
        if mpris is not None:
            mpris.publish(current_meta, url, paused=False)
        if last_payload is None:
            return
        payload = dict(last_payload)
        payload["large_image"] = fresh_image_url(url)
        # the image-less publish dropped the hover text (assets without
        # large_image render a "?" tile): restore it alongside the image
        value = slot_value(config.get().get("large_text"), current_meta)
        if value:
            payload["large_text"] = value
        log.info("cover art attached: %s", url)
        bridge.update(payload, throttle=RESUME_THROTTLE, force=True)
        last_payload = payload
        state.set_payload(payload)

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

    def handle_job(job: "str | tuple") -> None:
        nonlocal last_job_at, last_seen_seek, active
        if isinstance(job, tuple):
            _, key, url = job
            apply_art(key, url)
            return
        if job == "clear":
            log.info("metadata file gone or empty, clearing presence")
            clear_presence(paused=False)
            active = False
            return
        if job in ("update", "refresh"):
            last_job_at = time.monotonic()
            if not meta_path.exists():
                return
            meta = parse_metadata(meta_path)
            if meta is None:
                return
            if not meta.get("title"):
                log.info("no track in metadata, clearing presence")
                clear_presence(paused=False)
                active = False
                return
            send_update(
                meta,
                wait_progress=(job == "update"),
                throttle=(RESUME_THROTTLE if job == "refresh" else None),
            )
            last_seen_seek = progress.seek_seq
            active = True

    while not stop_event.is_set():
        try:
            first = jobs.get(timeout=0.5)
        except queue.Empty:
            first = None
        if first is not None:
            batch: "list[str | tuple]" = [first]
            try:
                while True:
                    nxt = jobs.get_nowait()
                    # consecutive plain jobs collapse into the newest (burst
                    # coalescing), but an art result never swallows a pending
                    # update/clear — dropping that would skip a publish
                    if isinstance(nxt, str) and isinstance(batch[-1], str):
                        batch[-1] = nxt
                    else:
                        batch.append(nxt)
            except queue.Empty:
                pass
            for job in batch:
                handle_job(job)
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
            mapping = config.get()
            if current_meta is not None:
                # non-blocking cache lookup: a missing/expired link no longer
                # uploads inline (that could stall the worker for the whole
                # upload+verify chain) — the art thread re-resolves instead
                image_url = artwork.cached_url(current_meta)
                if image_url != current_image:
                    current_image = image_url
                    if mpris is not None:
                        mpris.publish(current_meta, image_url, paused=False)
                if image_url:
                    payload["large_image"] = fresh_image_url(image_url)
                    value = slot_value(mapping.get("large_text"), current_meta)
                    if value:
                        payload["large_text"] = value
                    else:
                        payload.pop("large_text", None)
                else:
                    # no public art url: send no assets at all, an assets
                    # block without large_image renders a "?" tile
                    payload.pop("large_image", None)
                    payload.pop("large_text", None)
                    art_worker.request(current_meta)
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
