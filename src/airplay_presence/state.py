from __future__ import annotations

import logging
import threading
import time

from .constants import SEEK_BACKWARD_JUMP, SEEK_BASE_MAX_AGE, SEEK_FORWARD_JUMP


log = logging.getLogger("airplay-presence")

class ProgressState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._position = 0.0
        self._duration = 0.0
        self._at = 0.0
        self._last_raw_position: float | None = None
        self._seek_base: tuple[float, float] | None = None
        self._seek_base_at = 0.0
        # two-stage seek confirmation: a jump print only arms a candidate;
        # seek_seq bumps when the NEXT print continues forward from it. A
        # print returning to the pre-jump timeline is jitter, never a seek —
        # a counter oscillating near 0 used to bump seek_seq every other
        # print and the seek re-sends reset Discord's elapsed timer forever.
        self._pending_jump: tuple[float, float, float] | None = None  # (pos, dur, at)
        self.ever = False
        self.seek_seq = 0

    def _continues_seek_base(self, position: float, now: float) -> bool:
        """Does this print continue the pre-jump timeline (a jitter blip)?"""
        if self._seek_base is None or self._seek_base_at <= 0.0:
            return False
        base_pos = self._seek_base[0]
        expected = base_pos + (now - self._seek_base_at)
        return base_pos - SEEK_BACKWARD_JUMP <= position <= expected + SEEK_FORWARD_JUMP

    def set(self, position: float, duration: float) -> None:
        with self._lock:
            now = time.monotonic()
            prev = self._last_raw_position
            jump = prev is not None and (
                position > prev + SEEK_FORWARD_JUMP
                or position < prev - SEEK_BACKWARD_JUMP
            )
            candidate = self._pending_jump
            self._pending_jump = None
            blip = False
            if candidate is not None:
                cpos, cat = candidate[0], candidate[2]
                fresh = now - cat <= SEEK_BASE_MAX_AGE
                if fresh and not jump and cpos - 1.0 <= position <= cpos + (now - cat) + 1.0:
                    # the next print continues forward from the jump: confirmed
                    self.seek_seq += 1
                elif jump and self._continues_seek_base(position, now):
                    blip = True  # jitter blip: back on the pre-jump timeline, no bump
                # else: drop the candidate (expired or unrelated print)
            if jump and not blip:
                if candidate is None:
                    # first jump of a disturbance: arm with the pre-jump
                    # snapshot — same anchor a confirmed seek gets, and what
                    # the grace wait compares new-track prints against
                    self._seek_base = (self._position, self._duration)
                    self._seek_base_at = self._at
                self._pending_jump = (position, duration, now)
            self._last_raw_position = position
            self._position = position
            self._duration = duration
            self._at = now
            self.ever = True

    def seek_base(self, max_age: float) -> tuple[float, float] | None:
        with self._lock:
            if self._seek_base is None or (time.monotonic() - self._seek_base_at) > max_age:
                return None
            return self._seek_base

    def stale(self, timeout: float) -> bool:
        with self._lock:
            return self.ever and (time.monotonic() - self._at) > timeout

    def snapshot(
        self,
        max_age: float | None = None,
        min_at: float | None = None,
        raw: bool = False,
    ) -> tuple[float, float] | None:
        with self._lock:
            if self._duration <= 0:
                return None
            age = time.monotonic() - self._at
            if max_age is not None and age > max_age:
                return None
            if min_at is not None and self._at < min_at:
                return None
            if raw:
                return self._position, self._duration
            position = min(self._position + max(0.0, min(age, 3.0)), self._duration)
            return position, self._duration


class AppState:
    def __init__(self, progress: ProgressState):
        self._lock = threading.Lock()
        self._progress = progress
        self._meta: dict | None = None
        self._image: str | None = None
        self._paused = False
        self._status_line: str | None = None
        self._payload: dict | None = None

    def set_payload(self, payload: dict | None) -> None:
        with self._lock:
            self._payload = dict(payload) if payload else None

    def payload(self) -> dict | None:
        with self._lock:
            return dict(self._payload) if self._payload else None

    def set_now_playing(self, meta: dict, image: str | None) -> None:
        with self._lock:
            self._meta = dict(meta)
            self._image = image
            self._paused = False

    def set_status_line(self, line: str | None) -> None:
        with self._lock:
            self._status_line = line

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
            status_line = self._status_line
        if not meta:
            return {"playing": False, "paused": False, "status_line": status_line}
        progress = self._progress.snapshot()
        return {
            "playing": not paused,
            "paused": paused,
            "status_line": status_line,
            "title": meta.get("title"),
            "artist": meta.get("artist") or "",
            "album": meta.get("album") or "",
            "position": progress[0] if progress else None,
            "duration": progress[1] if progress else None,
            "image": image or "/art",
        }
