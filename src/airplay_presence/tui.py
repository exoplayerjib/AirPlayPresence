"""Curses terminal UI for airplay-presence.

The main pane shows live details (track, exact payload keys, timing bar);
the footer has three buttons — Log, Web UI, Quit — each clickable with a
key alias (L / W / Q). Pressing Log (or L) opens the log side panel: a
live, follow-aware scrolling view of the same lines the plain-log mode
prints. The curses loop runs on a dedicated daemon thread; all other
threads emit through RingBufferHandler and stay untouched.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("airplay-presence")


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def detail_lines(state, extras: dict, width: int) -> list[str]:
    snap = state.snapshot()
    payload = state.payload() or {}
    lines: list[str] = []

    def wrap(label: str, value) -> None:
        v = "-" if value in (None, "") else str(value)
        prefix = f"  {label:<24}"
        avail = max(12, width - len(prefix) - 1)
        for i in range(0, len(v), avail):
            lines.append(prefix + v[i : i + avail])
            prefix = " " * (len(label) + 14)

    def header(title: str) -> None:
        lines.append("")
        lines.append(f" {title.upper()}")

    if snap.get("playing"):
        st = "paused" if snap.get("paused") else "playing"
    else:
        st = "idle"
    header("now playing")
    lines.append(f"  {'playback':<24}{st}")
    if snap.get("title"):
        wrap("title", snap.get("title"))
        wrap("artist", snap.get("artist"))
        wrap("album", snap.get("album"))
        pos, dur = snap.get("position"), snap.get("duration")
        if pos is not None and dur:
            frac = max(0.0, min(1.0, pos / dur))
            span = max(10, min(40, width - 48))
            n = int(frac * span)
            bar = "#" * n + "-" * (span - n)
            lines.append(f"  {'progress':<24}{fmt_time(pos)} / {fmt_time(dur)}  [{bar}]")

    header("discord payload")
    for key in ("name", "details", "state", "large_text", "large_image"):
        v = payload.get(key)
        if key == "large_image" and v:
            v = f"{str(v)[:52]} …({len(str(v))} chars)"
        wrap(key, v)
    if payload.get("start"):
        note = ""
        if payload.get("end"):
            remaining = payload["end"] - int(time.time())
            note = f" ({remaining}s left)" if remaining > 0 else " (ended)"
        wrap("timer", f"start {payload['start']} → end {payload.get('end', '?')}{note}")
    else:
        wrap("timer", "none")
    wrap("member-list line", snap.get("status_line"))

    header("session")
    uxplay = extras.get("uxplay")
    proc = uxplay[0] if uxplay else None
    if proc is None:
        wrap("uxplay", "disabled (--no-uxplay)")
    elif proc.poll() is None:
        wrap("uxplay", f"running (pid {proc.pid})")
    else:
        wrap("uxplay", "terminated")
    bridge = extras.get("bridge")
    if bridge is not None:
        cb_mode = "dry-run" if bridge.dry_run else ("connected" if bridge.connected else "connecting")
        age = bridge.last_send_age
        age_text = "never" if age is None else f"{age:.0f}s ago"
        wrap("discord", f"{cb_mode}, last send {age_text}")
    mpris = extras.get("mpris")
    if mpris is None:
        wrap("mpris", "disabled")
    else:
        wrap("mpris", "connected" if mpris.is_connected else "unavailable")
    wrap("web ui", extras.get("webui_url") or "disabled (--no-ui)")
    wrap("config", extras.get("config_path"))
    cover = extras.get("cover_path")
    if cover is None:
        wrap("cover art", "unknown")
    else:
        from .artwork import valid_art_bytes

        try:
            data = cover.read_bytes()
        except OSError:
            data = b""
        if not data:
            wrap("cover art", f"missing ({cover})")
        else:
            size = len(data)
            try:
                age = time.time() - cover.stat().st_mtime
                age_text = f", changed {fmt_time(age)} ago"
            except OSError:
                age_text = ""
            state_txt = "valid JPEG" if valid_art_bytes(data) else ("placeholder/wrong format" if size < 200 else "not a valid JPEG")
            wrap("cover art", f"{size} bytes, {state_txt}{age_text}")

    header("cover art cache")
    uploader, resolver = extras.get("uploader"), extras.get("resolver")
    if uploader is None:
        wrap("upload", "disabled (--no-image)")
    else:
        entries = uploader.cache_info()
        if not entries:
            wrap("upload", "empty")
        for digest, age in entries[:4]:
            wrap(digest, f"age {fmt_time(age)}, TTL left {fmt_time(max(0, 3600 - age))}")
    if resolver is not None:
        remaining = resolver.retry_gate_remaining()
        kind = resolver.last_failure_kind
        status = getattr(resolver, "last_http_status", None)
        gate_text = {"no-file": "no art file yet - next heartbeat retries", "disabled": "uploads off"}.get(kind)
        if remaining > 0 and kind == "network":
            suffix = f" (HTTP {status})" if status else " (network)"
            wrap("retry gate", f"cooldown {remaining:.0f}s{suffix}")
        elif gate_text and remaining <= 0:
            wrap("retry gate", f"open - {gate_text}")
        else:
            wrap("retry gate", "open" if remaining <= 0 else f"cooldown {remaining:.0f}s")
        for key, age in resolver.cache_info()[:4]:
            wrap(key, f"age {fmt_time(age)}")
    return lines


def run(stdscr, state, ring, extras, stop_event, ready=None) -> None:
    """Curses main loop. Returns when stop_event is set or the user quits."""
    import curses as C

    ui = {"mode": "details", "detail_scroll": 0, "log_scroll": 0, "log_follow": True, "want_quit": False}
    buttons: list[tuple[int, int, str]] = []

    def draw_log_panel(stdscr, rows: int, cols: int, x: int) -> None:
        all_lines = ring.snapshot()
        avail = rows - 4
        if avail <= 0:
            return
        if ui["log_follow"] or ui["log_scroll"] <= 0:
            view = all_lines[-avail:]
        else:
            end = max(0, len(all_lines) - ui["log_scroll"])
            view = all_lines[max(0, end - avail) : end]
        for i, line in enumerate(view):
            attr = C.A_NORMAL
            if " ERROR" in line.formatted:
                attr = C.color_pair(2) | C.A_BOLD
            elif " WARNING" in line.formatted:
                attr = C.color_pair(1)
            try:
                stdscr.addnstr(1 + i, x, line.formatted, cols - x - 1, attr)
            except C.error:
                pass
        marker = "follow ▼" if ui["log_follow"] else f"{ui['log_scroll']} from bottom, G to follow"
        try:
            stdscr.addnstr(rows - 2, x, f"Log {marker}", cols - x - 1, C.A_DIM)
        except C.error:
            pass

    def footer(stdscr, rows: int, cols: int) -> None:
        buttons.clear()
        labels = ["L Log", "W Web UI", "Q Quit"] if ui["mode"] == "details" else ["L Details", "W Web UI", "Q Quit"]
        x = 1
        for lb in labels:
            try:
                stdscr.addnstr(rows - 1, x, lb, len(lb), C.A_REVERSE)
            except C.error:
                pass
            buttons.append((x, x + len(lb), lb.lstrip()[0].lower()))
            x += len(lb) + 2
        hint = "↑↓/jk scroll" if ui["mode"] == "details" else "G follow bottom"
        try:
            stdscr.addnstr(rows - 1, x, hint, max(0, cols - x - 1), C.A_DIM)
        except C.error:
            pass

    def draw() -> None:
        rows, cols = stdscr.getmaxyx()
        stdscr.erase()
        panel = ui["mode"] == "log"
        left_w = max(24, cols // 2) if panel else cols
        title = state.snapshot().get("title") or "nothing playing"
        try:
            stdscr.addnstr(0, 0, f" AirPlayPresence — {title} ", cols - 1, C.A_BOLD)
        except C.error:
            pass
        body = detail_lines(state, extras, left_w - 2)
        visible = rows - 3
        max_scroll = max(0, len(body) - visible)
        ui["detail_scroll"] = max(0, min(ui["detail_scroll"], max_scroll))
        for i, text in enumerate(body[ui["detail_scroll"] : ui["detail_scroll"] + visible]):
            try:
                stdscr.addnstr(1 + i, 0, text, left_w - 1)
            except C.error:
                pass
        if max_scroll > 0:
            hint = ("↓ more " if ui["detail_scroll"] < max_scroll else "") + (
                f"↑ ({ui['detail_scroll']}/{max_scroll})" if ui["detail_scroll"] > 0 else ""
            )
            if hint:
                try:
                    stdscr.addnstr(rows - 2, 1, hint, left_w - 2, C.A_DIM)
                except C.error:
                    pass
        if panel:
            try:
                stdscr.vline(0, left_w, C.ACS_VLINE, rows)
            except C.error:
                pass
            draw_log_panel(stdscr, rows, cols, left_w + 1)
        footer(stdscr, rows, cols)

    def activate(act: str) -> None:
        if act == "l":
            ui["mode"] = "log" if ui["mode"] == "details" else "details"
            if ui["mode"] == "log":
                ui["log_follow"] = True
                ui["log_scroll"] = 0
        elif act == "w":
            fn = extras.get("open_webui")
            if fn:
                try:
                    fn()
                except Exception as exc:
                    log.warning("opening web UI failed: %s", exc)
        elif act == "q":
            ui["want_quit"] = True

    def scroll(step: int) -> None:
        # positive step moves toward newer/lower content
        if ui["mode"] == "log":
            if step > 0:
                ui["log_follow"] = ui["log_scroll"] - step <= 0
                ui["log_scroll"] = max(0, ui["log_scroll"] - step)
            else:
                ui["log_scroll"] += -step
                ui["log_follow"] = False
        else:
            ui["detail_scroll"] = max(0, ui["detail_scroll"] + step)

    def to_bottom() -> None:
        ui["log_follow"] = True
        ui["log_scroll"] = 0

    if ready is not None:
        ready.set()
    stdscr.keypad(True)
    try:
        C.curs_set(0)
    except C.error:
        pass
    try:
        C.start_color()
        C.use_default_colors()
        C.init_pair(1, C.COLOR_YELLOW, -1)
        C.init_pair(2, C.COLOR_RED, -1)
    except C.error:
        pass
    try:
        C.mousemask(C.ALL_MOUSE_EVENTS)
    except C.error:
        pass

    stdscr.timeout(250)
    while not stop_event.is_set() and not ui["want_quit"]:
        draw()
        key = stdscr.getch()
        if key == C.ERR:
            continue
        if key == C.KEY_MOUSE:
            try:
                _, mx, my, _, _ = C.getmouse()
            except C.error:
                continue
            if my == stdscr.getmaxyx()[0] - 1:
                for x0, x1, act in buttons:
                    if x0 <= mx < x1:
                        activate(act)
                        break
            continue
        if key == C.KEY_RESIZE:
            continue
        if key in (ord("q"), ord("Q"), 27):
            ui["want_quit"] = True
        elif key in (ord("l"), ord("L")):
            activate("l")
        elif key in (ord("w"), ord("W")):
            activate("w")
        elif key in (ord("j"), C.KEY_DOWN):
            scroll(+1)
        elif key in (ord("k"), C.KEY_UP):
            scroll(-1)
        elif key == C.KEY_NPAGE:
            scroll(+10)
        elif key == C.KEY_PPAGE:
            scroll(-10)
        elif key in (C.KEY_END, ord("G")):
            to_bottom()
        elif key == C.KEY_HOME and ui["mode"] != "log":
            ui["detail_scroll"] = 0
