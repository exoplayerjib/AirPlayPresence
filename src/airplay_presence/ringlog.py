"""In-memory log ring buffer shared by the terminal UI."""

from __future__ import annotations

import collections
import logging
import threading


class LogLine:
    __slots__ = ("formatted", "seq")

    def __init__(self, formatted: str, seq: int):
        self.formatted = formatted
        self.seq = seq


class RingBufferHandler(logging.Handler):
    """Append formatted records to a bounded deque; wake the UI for each line."""

    def __init__(self, capacity: int = 500, level: int = logging.INFO):
        super().__init__(level=level)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        self._lines: collections.deque[LogLine] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._wake = threading.Event()

    @property
    def wake(self) -> threading.Event:
        return self._wake

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            return
        with self._lock:
            self._seq += 1
            self._lines.append(LogLine(text, self._seq))
        self._wake.set()

    def snapshot(self) -> list[LogLine]:
        with self._lock:
            return list(self._lines)

    def last_seq(self) -> int:
        with self._lock:
            return self._seq
