from __future__ import annotations

import logging
import json
import threading
from pathlib import Path

from .constants import (
    APPLE_MUSIC, CUSTOM_PREFIX, DEFAULT_CONFIG, ROTATION_MAX_INTERVAL,
    ROTATION_MIN_INTERVAL, SOURCE_FIELDS, STATUS_DISPLAY_OPTIONS, SLOTS,
)


log = logging.getLogger("airplay-presence")

def valid_source(value) -> bool:
    if value in SOURCE_FIELDS or value in ("none", APPLE_MUSIC):
        return True
    return isinstance(value, str) and value.startswith(CUSTOM_PREFIX) and bool(value[len(CUSTOM_PREFIX):].strip())


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
        mapping["status_rotation"] = bool(data.get("status_rotation"))
        order = data.get("status_rotation_order")
        if isinstance(order, list):
            mapping["status_rotation_order"] = list(
                dict.fromkeys(v for v in order if v in STATUS_DISPLAY_OPTIONS)
            )
        interval = data.get("status_rotation_interval")
        if isinstance(interval, int):
            mapping["status_rotation_interval"] = max(
                ROTATION_MIN_INTERVAL, min(ROTATION_MAX_INTERVAL, interval)
            )
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
