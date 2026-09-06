from __future__ import annotations

import logging
import time
import unicodedata

from .constants import FIELD_LIMIT
from pathlib import Path

log = logging.getLogger("airplay-presence")

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
