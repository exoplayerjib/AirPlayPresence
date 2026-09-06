from __future__ import annotations

import logging
import asyncio
import threading

try:
    from dbus_next import PropertyAccess, RequestNameReply
    from dbus_next.aio import MessageBus
    from dbus_next.errors import DBusError
    from dbus_next.service import ServiceInterface, dbus_property, method
    from dbus_next.signature import Variant
    MPRIS_AVAILABLE = True
except ImportError:
    MPRIS_AVAILABLE = False

from .constants import MPRIS_NAME, MPRIS_PATH
from .utils import clip


log = logging.getLogger("airplay-presence")

class _MprisRoot(ServiceInterface):
    def __init__(self, server: "MprisServer") -> None:
        super().__init__("org.mpris.MediaPlayer2")
        self._server = server

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanSetFullscreen(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "AirPlayPresence"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class _MprisPlayer(ServiceInterface):
    def __init__(self, server: "MprisServer") -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._server = server

    def _reject(self) -> None:
        raise DBusError(
            "org.freedesktop.DBus.Error.NotSupported",
            "the AirPlay client controls playback, not the server",
        )

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._server.playback_status()

    @dbus_property(access=PropertyAccess.READ)
    def LoopStatus(self) -> "s":
        return "None"

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def Shuffle(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._server.metadata()

    @dbus_property(access=PropertyAccess.READ)
    def Volume(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return self._server.position_us()

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return False

    @method()
    def Play(self):
        self._reject()

    @method()
    def Pause(self):
        self._reject()

    @method()
    def PlayPause(self):
        self._reject()

    @method()
    def Stop(self):
        self._reject()

    @method()
    def Next(self):
        self._reject()

    @method()
    def Previous(self):
        self._reject()

    @method()
    def Seek(self, offset: "x"):
        self._reject()

    @method()
    def SetPosition(self, track_id: "o", position: "x"):
        self._reject()

    @method()
    def OpenUri(self, uri: "s"):
        self._reject()


class MprisServer:
    def __init__(self, ui_port: int | None, image_path: Path, progress: ProgressState) -> None:
        self._ui_port = ui_port
        self._image_path = image_path
        self._progress = progress
        self._meta: dict | None = None
        self._image: str | None = None
        self._paused = False
        self._connected = False
        self._bus = None
        self._root = _MprisRoot(self)
        self._player = _MprisPlayer(self)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mpris", daemon=True)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            self._connected = bool(future.result(timeout=15))
        except Exception as exc:
            log.warning("MPRIS setup failed: %s", exc)
        if self._connected:
            log.info("MPRIS player on the session bus: %s", MPRIS_NAME)
        else:
            log.warning("MPRIS disabled (session bus unavailable)")

    async def _connect(self) -> bool:
        try:
            bus = MessageBus()
            await bus.connect()
            bus.export(MPRIS_PATH, self._root)
            bus.export(MPRIS_PATH, self._player)
            reply = await bus.request_name(MPRIS_NAME)
            if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
                log.warning("MPRIS name %s not acquired (%s)", MPRIS_NAME, reply)
                return False
            self._bus = bus
            return True
        except Exception as exc:
            log.warning("MPRIS unavailable: %s", exc)
            return False

    def publish(self, meta: dict | None, image_url: str | None, paused: bool) -> None:
        if not self._connected:
            return
        self._meta = dict(meta) if meta else None
        self._image = image_url
        self._paused = paused
        asyncio.run_coroutine_threadsafe(self._push(), self._loop)

    async def _push(self) -> None:
        try:
            self._player.emit_properties_changed(
                {"Metadata": self.metadata(), "PlaybackStatus": self.playback_status()}
            )
        except Exception as exc:
            log.debug("mpris: push failed: %s", exc)

    def playback_status(self) -> str:
        if self._meta is None:
            return "Stopped"
        return "Paused" if self._paused else "Playing"

    def position_us(self) -> int:
        snap = self._progress.snapshot()
        return int(snap[0] * 1_000_000) if snap else 0

    def _art_url(self) -> str | None:
        if self._image:
            return self._image
        if self._ui_port is not None:
            return f"http://127.0.0.1:{self._ui_port}/art"
        return "file://" + str(self._image_path.resolve())

    def metadata(self) -> dict:
        meta = self._meta
        if not meta:
            return {}
        fields: dict = {}
        for key, slot in (("title", "xesam:title"), ("album", "xesam:album")):
            value = clip(meta.get(key))
            if value:
                fields[slot] = Variant("s", value)
        for key, slot in (
            ("artist", "xesam:artist"),
            ("album artist", "xesam:albumArtist"),
            ("genre", "xesam:genre"),
        ):
            value = clip(meta.get(key))
            if value:
                fields[slot] = Variant("as", [value])
        art = self._art_url()
        if art:
            fields["mpris:artUrl"] = Variant("s", art)
        snap = self._progress.snapshot()
        if snap:
            fields["mpris:length"] = Variant("x", int(snap[1] * 1_000_000))
        return fields
