from __future__ import annotations

import re

from pypresence import StatusDisplayType

FIELD_LIMIT = 128
UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
UPLOAD_EXPIRY = "1h"
UPLOAD_TTL = 60 * 60
UPLOAD_RETRY = 60.0
UPLOAD_RETRY_TRANSIENT = 20.0
UPLOAD_GATE_EXTENDED_SECONDS = 300.0
UPLOAD_GATE_EXTENDED_STRIKES = 3
UPLOAD_HTTP_FAILURE_BODY_CAPTURE = 1000
UPLOAD_HTTP_RETRIES = 2
UPLOAD_HTTP_RETRY_BACKOFF = 2.0
UPLOAD_VERIFY_TIMEOUT = 10.0
UPLOAD_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
SPOTIFY_TOKEN_MARGIN = 60.0
DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"
PAUSE_TIMEOUT = 5.0
RESUME_THROTTLE = 2.0
PROGRESS_WAIT = 1.0
SEEK_FORWARD_JUMP = 4.0
SEEK_BACKWARD_JUMP = 2.0
SEEK_BASE_MAX_AGE = 8.0
HEARTBEAT_QUIET = 5.0
MIN_ART_BYTES = 2000
ART_SETTLE_TIMEOUT = 5.0
MPRIS_NAME = "org.mpris.MediaPlayer2.airplaypresence"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
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
CUSTOM_PREFIX = "custom:"
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
    "status_rotation": False,
    "status_rotation_order": ["name", "details", "state"],
    "status_rotation_interval": 30,
}
ROTATION_MIN_INTERVAL = 10
ROTATION_MAX_INTERVAL = 600
