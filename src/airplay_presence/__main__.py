from __future__ import annotations
import collections
import logging
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from watchdog.observers import Observer

from .ringlog import RingBufferHandler
from .artwork import ArtworkResolver, ImageUploader
from .cli import parse_args
from .config import PresenceConfig
from .constants import PAUSE_TIMEOUT
from .mpris import MPRIS_AVAILABLE, MprisServer
from .presence import PresenceBridge
from .state import AppState, ProgressState
from .uxplay import read_uxplay_output, spawn_uxplay
from .webui import make_ui_server
from .worker import Debouncer, MetaFileHandler, run_worker

log = logging.getLogger("airplay-presence")


def _setup_logging(args, stdout_is_tty: bool) -> "RingBufferHandler | None":
    level = logging.DEBUG if args.verbose else logging.INFO
    if not args.no_tui and stdout_is_tty:
        try:
            import curses  # noqa: F401
        except ImportError:
            stdout_is_tty = False
    if not args.no_tui and stdout_is_tty:
        from .ringlog import RingBufferHandler

        ring = RingBufferHandler(level=level)
        logging.basicConfig(level=level, handlers=[ring], force=True)
        return ring
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return None


def main() -> int:
    args = parse_args()
    stdout_is_tty = False
    try:
        stdout_is_tty = sys.stdout.isatty()
    except Exception:
        pass
    ring = _setup_logging(args, stdout_is_tty)
    meta_path = Path(args.meta_file)
    image_path = Path(args.image_file)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser() if args.config else Path.home() / ".config" / "airplay-presence" / "config.json"
    if not args.config and not config_path.exists():
        legacy = Path.home() / ".config" / "uxplay-presence" / "config.json"
        if legacy.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(config_path))
            log.info("migrated config from %s", legacy)
            try:
                legacy.parent.rmdir()
            except OSError:
                pass

    progress = ProgressState()
    state = AppState(progress)
    config = PresenceConfig(config_path)
    bridge = PresenceBridge(args.client_id or "", args.min_interval, args.dry_run)
    artwork = ArtworkResolver(ImageUploader(not args.no_image), itunes=args.itunes_art)
    jobs: "queue.Queue[str]" = queue.Queue()
    stop_event = threading.Event()
    failed = threading.Event()
    crashed = threading.Event()
    misses = [0]
    debouncer = Debouncer(args.debounce, jobs.put)

    ui_server = None
    if not args.no_ui:
        ui_server = make_ui_server(args.ui_port, state, image_path, config, lambda: jobs.put("refresh"))

    mpris = None
    if not args.no_mpris:
        if not MPRIS_AVAILABLE:
            log.warning("MPRIS disabled (dbus-next is not installed)")
        else:
            mpris = MprisServer(None if args.no_ui else args.ui_port, image_path, progress)
            mpris.start()

    proc: subprocess.Popen | None = None
    tail: collections.deque = collections.deque(maxlen=15)
    uxplay_dead = threading.Event()
    if not args.no_uxplay:
        proc = spawn_uxplay(args, meta_path, image_path)
        reader = threading.Thread(
            target=read_uxplay_output, args=(proc, progress, tail, uxplay_dead), daemon=True
        )
        reader.start()
        time.sleep(1.0)
        if proc.poll() is not None:
            log.error("uxplay exited immediately; recent output:\n%s", "\n".join(tail) or "(none)")
            return 1
        log.info("uxplay running (pid %d)", proc.pid)
        if ui_server is not None and not args.no_browser:
            url = f"http://127.0.0.1:{args.ui_port}"
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    bridge.start(failed, stop_event)
    worker = threading.Thread(
        target=run_worker,
        args=(meta_path, image_path, bridge, artwork, config, jobs, stop_event, state, progress, misses, mpris, debouncer),
        daemon=True,
    )
    worker.start()

    observer = Observer()
    observer.schedule(MetaFileHandler(meta_path.name, debouncer), str(meta_path.parent), recursive=False)
    observer.start()
    log.info("watching %s", meta_path)
    jobs.put("update")

    def shutdown(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    def check_uxplay_alive() -> None:
        if uxplay_dead.is_set() and proc is not None and proc.poll() is not None:
            log.error("uxplay terminated unexpectedly; recent output:\n%s", "\n".join(tail) or "(none)")
            crashed.set()
            stop_event.set()

    def fallback_logging() -> None:
        level = logging.DEBUG if args.verbose else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
            force=True,
        )

    def open_webui() -> None:
        if ui_server is None:
            log.warning("web UI disabled (--no-ui)")
            return
        url = f"http://127.0.0.1:{args.ui_port}"
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    try:
        tui_active = False
        if ring is not None and not failed.is_set() and not crashed.is_set():
            tui_extras = {
                "uxplay": (proc, uxplay_dead) if proc is not None else None,
                "bridge": bridge,
                "mpris": mpris,
                "webui_url": None if ui_server is None else f"http://127.0.0.1:{args.ui_port}",
                "config_path": str(config_path),
                "cover_path": image_path,
                "uploader": artwork.uploader,
                "resolver": artwork,
                "open_webui": open_webui,
            }
            ready = threading.Event()
            boot_err = []

            def _run_tui() -> None:
                import curses

                from . import tui

                try:
                    curses.wrapper(tui.run, state, ring, tui_extras, stop_event, ready)
                except Exception as exc:
                    boot_err.append(exc)
                finally:
                    stop_event.set()

            tui_thread = threading.Thread(target=_run_tui, name="tui", daemon=True)
            tui_thread.start()
            ready.wait(timeout=3.0)
            if ready.is_set():
                tui_active = True
                while not stop_event.is_set():
                    time.sleep(0.5)
                    check_uxplay_alive()
            else:
                reason = boot_err[0].__class__.__name__ if boot_err else "no output"
                fallback_logging()
                log.warning("TUI could not start (%s); falling back to plain log", reason)
        if not tui_active:
            while not stop_event.is_set():
                time.sleep(0.5)
                check_uxplay_alive()
    finally:
        debouncer.cancel()
        observer.stop()
        observer.join(timeout=5)
        stop_event.set()
        worker.join(timeout=5)
        if ui_server is not None:
            ui_server.shutdown()
            ui_server.server_close()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return 1 if (failed.is_set() or crashed.is_set()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
