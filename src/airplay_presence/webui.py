from __future__ import annotations

import logging
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .artwork import valid_art_bytes
from .config import (
    ROTATION_MAX_INTERVAL, ROTATION_MIN_INTERVAL, STATUS_DISPLAY_OPTIONS, SLOTS, valid_source,
)
from .state import AppState


log = logging.getLogger("airplay-presence")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Now Playing</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:#0f1115;color:#e8eaf0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
.card{width:min(380px,92vw);padding:28px;border-radius:18px;background:#171a21;box-shadow:0 10px 40px rgba(0,0,0,.55);text-align:center}
img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;background:#20242e}
h1{font-size:1.3rem;margin:18px 0 4px;overflow-wrap:anywhere}
.artist{margin:0;color:#a3aabb;overflow-wrap:anywhere}
.album{margin:4px 0 0;color:#6b7280;font-size:.85rem;overflow-wrap:anywhere}
.paused-label{display:none;margin:14px 0 0;color:#f0b232;font-size:.85rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.bar{height:6px;border-radius:3px;background:#262b36;margin:22px 0 8px;overflow:hidden}
.bar>div{height:100%;width:0;border-radius:3px;background:#5865f2;transition:width 1s linear}
.times{display:flex;justify-content:space-between;font-size:.8rem;color:#a3aabb;font-variant-numeric:tabular-nums}
#idle p{margin:0;color:#a3aabb}
#idle .sub{color:#6b7280;font-size:.85rem;margin-top:6px}
.settings{width:min(380px,92vw);border-radius:12px;background:#171a21;color:#a3aabb;font-size:.85rem}
.settings summary{cursor:pointer;padding:12px 16px;color:#a3aabb;user-select:none}
.settings .row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 16px}
.settings select{background:#262b36;color:#e8eaf0;border:1px solid #33384a;border-radius:6px;padding:4px 8px;font-size:.85rem}
.settings input{background:#262b36;color:#e8eaf0;border:1px solid #33384a;border-radius:6px;padding:4px 8px;font-size:.85rem;width:118px}
.rotate-box{margin:8px 16px 12px;padding:10px;border:1px solid #33384a;border-radius:8px}
.rotate-head{display:flex;align-items:center;gap:8px;color:#e8eaf0;cursor:pointer;user-select:none}
.rotate-head input{width:auto}
#rot-list{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
#rot-list li{display:flex;align-items:center;gap:8px;padding:6px 8px;background:#262b36;border:1px solid #33384a;border-radius:6px;cursor:grab;user-select:none}
#rot-list li.off{opacity:.45;cursor:default}
#rot-list li.drag{opacity:.5;border-style:dashed}
#rot-list li.over{border-color:#5865f2}
#rot-list .grip{color:#6b7280;cursor:grab}
#rot-list .live{margin-left:auto;color:#23a55a;font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.rotate-box .row{padding:8px 0 0}
</style>
</head>
<body>
<div class="card">
  <div id="idle"><p>Nothing playing</p><p class="sub">Start an AirPlay session to this server.</p></div>
  <div id="now" style="display:none">
    <img id="art" alt="cover art">
    <p class="paused-label" id="paused">Paused</p>
    <h1 id="title"></h1>
    <p class="artist" id="artist"></p>
    <p class="album" id="album"></p>
    <div class="bar"><div id="fill"></div></div>
    <div class="times" id="times"><span id="elapsed">0:00</span><span id="total">0:00</span></div>
  </div>
</div>
<details class="settings" id="settings">
  <summary>Discord presence lines</summary>
  <div class="row"><span>First line (Listening to &hellip;)</span><select id="sel-name"></select></div>
  <div class="row"><span>Second line</span><select id="sel-details"></select></div>
  <div class="row"><span>Third line</span><select id="sel-state"></select></div>
  <div class="row"><span>Fourth line (image text)</span><select id="sel-large_text"></select></div>
  <div class="row"><span>Member list shows</span><select id="sel-status_display"></select></div>
  <div class="rotate-box">
    <label class="rotate-head"><input type="checkbox" id="rot-enable"> Rotate member-list line</label>
    <ul id="rot-list"></ul>
    <div class="row"><span>Every</span>
      <select id="rot-interval">
        <option value="15">15 s</option>
        <option value="30">30 s</option>
        <option value="60">1 min</option>
        <option value="120">2 min</option>
        <option value="300">5 min</option>
      </select>
    </div>
  </div>
</details>
<script>
const el = id => document.getElementById(id);
const fmt = t => { t = Math.max(0, Math.floor(t)); return Math.floor(t/60) + ":" + String(t%60).padStart(2,"0"); };
const FIELDS = [["title","Title"],["artist","Artist"],["album","Album"],["album artist","Album artist"],["genre","Genre"],["apple music","Apple Music (fixed)"],["custom","Custom text…"],["none","(nothing)"]];
const SLOTS = [["name","sel-name"],["details","sel-details"],["state","sel-state"],["large_text","sel-large_text"]];
const DISPLAYS = [["name","First line (Apple Music)"],["details","Second line"],["state","Third line"]];
let cfg = null;
async function postConfig(next, fallback) {
  try {
    const r = await fetch("/config", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(next) });
    if (!r.ok) throw new Error("status " + r.status);
    cfg = await r.json();
    return true;
  } catch (e) { return false; }
}
const CUSTOM = "custom";
const inputs = {};
for (const [slot, sel] of SLOTS) {
  const s = el(sel);
  for (const [value, label] of FIELDS) {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    s.appendChild(o);
  }
  const inp = document.createElement("input");
  inp.type = "text"; inp.placeholder = "Custom text"; inp.style.display = "none";
  s.parentElement.appendChild(inp);
  inputs[slot] = inp;
  const revert = () => {
    const v = cfg && cfg[slot] ? cfg[slot] : "title";
    if (v.startsWith("custom:")) { s.value = CUSTOM; inp.value = v.slice(7); inp.style.display = ""; }
    else { s.value = v; inp.style.display = "none"; }
  };
  s.addEventListener("change", async () => {
    if (s.value === CUSTOM) { inp.style.display = ""; inp.focus(); return; }
    inp.style.display = "none";
    if (!await postConfig({ ...cfg, [slot]: s.value })) revert();
  });
  inp.addEventListener("change", async () => {
    const text = inp.value.trim();
    if (!text) { revert(); return; }
    if (!await postConfig({ ...cfg, [slot]: "custom:" + text })) revert();
  });
}
const selD = el("sel-status_display");
for (const [value, label] of DISPLAYS) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  selD.appendChild(o);
}
selD.addEventListener("change", async () => {
  if (!await postConfig({ ...cfg, status_display: selD.value })) selD.value = cfg ? cfg.status_display : "state";
});
const rotEnable = el("rot-enable");
const rotList = el("rot-list");
const rotInterval = el("rot-interval");
const rotOrder = () => [...rotList.children].filter(li => !li.classList.contains("off")).map(li => li.dataset.value);
let dragVal = null;
function renderRot() {
  const valid = DISPLAYS.map(d => d[0]);
  const saved = Array.isArray(cfg.status_rotation_order) ? cfg.status_rotation_order.filter(v => valid.includes(v)) : [];
  const enabled = [...new Set(saved)];
  const off = valid.filter(v => !enabled.includes(v));
  rotList.innerHTML = "";
  for (const value of [...enabled, ...off]) {
    const on = enabled.includes(value);
    const label = DISPLAYS.find(d => d[0] === value)[1];
    const li = document.createElement("li");
    li.dataset.value = value;
    li.draggable = on;
    if (!on) li.classList.add("off");
    const grip = document.createElement("span");
    grip.className = "grip"; grip.textContent = "⠿";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = on; cb.style.width = "auto";
    const lbl = document.createElement("span"); lbl.textContent = label;
    const live = document.createElement("span"); live.className = "live";
    li.append(grip, cb, lbl, live);
    cb.addEventListener("change", () => {
      let order = rotOrder();
      if (cb.checked) { if (!order.includes(value)) order.push(value); }
      else order = order.filter(v => v !== value);
      saveRot({ status_rotation_order: order });
    });
    li.addEventListener("dragstart", e => {
      dragVal = value; li.classList.add("drag");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", value); } catch (err) {}
    });
    li.addEventListener("dragend", () => {
      dragVal = null; li.classList.remove("drag");
      [...rotList.children].forEach(x => x.classList.remove("over"));
    });
    li.addEventListener("dragover", e => {
      if (dragVal && dragVal !== value && on) { e.preventDefault(); li.classList.add("over"); }
    });
    li.addEventListener("dragleave", () => li.classList.remove("over"));
    li.addEventListener("drop", e => {
      e.preventDefault(); li.classList.remove("over");
      if (!dragVal || dragVal === value || !on) return;
      const order = rotOrder();
      order.splice(order.indexOf(dragVal), 1);
      order.splice(order.indexOf(value), 0, dragVal);
      saveRot({ status_rotation_order: order });
    });
    rotList.appendChild(li);
  }
}
function syncRotUi() {
  const on = !!(cfg && cfg.status_rotation);
  selD.disabled = on;
  selD.parentElement.style.opacity = on ? ".45" : "";
  rotEnable.checked = on;
  rotInterval.value = String((cfg && cfg.status_rotation_interval) || 30);
}
async function saveRot(patch) {
  await postConfig({ ...cfg, ...patch });
  syncRotUi();
  renderRot();
}
rotEnable.addEventListener("change", () => saveRot({ status_rotation: rotEnable.checked }));
rotInterval.addEventListener("change", () => saveRot({ status_rotation_interval: parseInt(rotInterval.value, 10) }));
(async () => {
  try { cfg = await (await fetch("/config")).json(); } catch (e) { return; }
  for (const [slot, sel] of SLOTS) {
    const v = cfg[slot] || "title";
    if (v.startsWith("custom:")) { el(sel).value = CUSTOM; inputs[slot].value = v.slice(7); inputs[slot].style.display = ""; }
    else el(sel).value = v;
  }
  selD.value = cfg.status_display || "state";
  syncRotUi();
  renderRot();
})();
function markLive(line) {
  [...rotList.children].forEach(li => {
    li.querySelector(".live").textContent = li.dataset.value === line ? "showing" : "";
  });
}
async function tick() {
  try {
    const s = await (await fetch("/state")).json();
    const visible = s.playing || s.paused;
    el("idle").style.display = visible ? "none" : "block";
    el("now").style.display = visible ? "block" : "none";
    markLive(visible ? s.status_line : null);
    if (!visible) return;
    el("paused").style.display = s.paused ? "block" : "none";
    if (el("art").dataset.src !== s.image) { el("art").src = s.image; el("art").dataset.src = s.image; }
    el("title").textContent = s.title || "";
    el("artist").textContent = s.artist || "";
    el("album").textContent = s.album || "";
    if (s.duration) {
      el("fill").style.width = Math.min(100 * s.position / s.duration, 100) + "%";
      el("elapsed").textContent = fmt(s.position);
      el("total").textContent = fmt(s.duration);
      el("times").style.display = "flex";
    } else {
      el("fill").style.width = "0%";
      el("times").style.display = "none";
    }
  } catch (e) {}
}
setInterval(tick, 1000);
tick();
</script>
</body>
</html>
"""

def make_ui_server(
    port: int,
    state: AppState,
    image_path: Path,
    config: PresenceConfig,
    on_config_change,
) -> ThreadingHTTPServer | None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            log.debug("ui: " + format, *args)

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif self.path == "/state":
                body = json.dumps(state.snapshot()).encode()
                self._send(200, "application/json", body)
            elif self.path == "/config":
                body = json.dumps(config.get()).encode()
                self._send(200, "application/json", body)
            elif self.path == "/art":
                try:
                    data = image_path.read_bytes()
                except OSError:
                    data = b""
                if not valid_art_bytes(data):
                    self.send_error(404)
                    return
                self._send(200, "image/jpeg", data)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/config":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
            except ValueError:
                self._send(400, "application/json", b'{"error": "invalid json"}')
                return
            if not isinstance(data, dict):
                self._send(400, "application/json", b'{"error": "invalid body"}')
                return
            mapping: dict[str, str] = {}
            for slot in SLOTS:
                value = data.get(slot)
                if not valid_source(value):
                    self._send(400, "application/json", b'{"error": "invalid fields"}')
                    return
                mapping[slot] = value
            display = data.get("status_display")
            if display is not None:
                if display not in STATUS_DISPLAY_OPTIONS:
                    self._send(400, "application/json", b'{"error": "invalid fields"}')
                    return
                mapping["status_display"] = display
            mapping.update(
                {k: v for k, v in config.get().items() if k.startswith("status_rotation")}
            )
            rotation = data.get("status_rotation")
            if rotation is not None:
                mapping["status_rotation"] = bool(rotation)
            order = data.get("status_rotation_order")
            if order is not None:
                if (
                    not isinstance(order, list)
                    or any(v not in STATUS_DISPLAY_OPTIONS for v in order)
                    or len(set(order)) != len(order)
                ):
                    self._send(400, "application/json", b'{"error": "invalid fields"}')
                    return
                mapping["status_rotation_order"] = order
            interval = data.get("status_rotation_interval")
            if interval is not None:
                if not isinstance(interval, int) or isinstance(interval, bool):
                    self._send(400, "application/json", b'{"error": "invalid fields"}')
                    return
                mapping["status_rotation_interval"] = max(
                    ROTATION_MIN_INTERVAL, min(ROTATION_MAX_INTERVAL, interval)
                )
            saved = config.set(mapping)
            on_config_change()
            self._send(200, "application/json", json.dumps(saved).encode())

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log.warning("web ui disabled: cannot bind port %d (%s)", port, exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("web ui at http://127.0.0.1:%d", port)
    return server
