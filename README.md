# AirPlayPresence

**A real Apple Music experience on Linux, with Discord Rich Presence.**

AirPlayPresence turns any Linux machine into an AirPlay audio receiver and pipes everything your iPhone, iPad, or Mac is playing straight into Discord — cover art, song, artist, album, and a live elapsed/remaining countdown — plus a local "Now Playing" web page and desktop media integration (MPRIS).

No more "Listening to Spotify" envy. Stream Apple Music to Linux, and Discord finally knows what you're hearing.

## Features

- **Self-contained AirPlay server** — spawns `uxplay` itself as a headless, audio-only receiver (no mirroring window); just pick it from the AirPlay menu on your device.
- **Discord Rich Presence** — "Listening to Apple Music" with cover art and a live elapsed / remaining timer.
- **Editable presence lines** — choose what goes on each of the four Discord lines (title / artist / album / album artist / genre / fixed text) and what the member-list badge shows, right from the web UI. Saved across restarts.
- **Cover art pipeline** — the validated cover file is uploaded to litterbox.catbox.moe (1-hour links); pass `--itunes-art` to try the iTunes CDN first.
- **MPRIS media player** — shows up in GNOME/KDE media controls and `playerctl` with title, artist, album, art, and position.
- **Honest pause handling** — pausing or ending the stream clears the presence; resuming restores it automatically.
- **Local web page** — cover art, track info, and a progress bar at `http://127.0.0.1:8080`.

## Requirements

- Linux (Avahi/mDNS enabled — standard on most desktops)
- Python **3.10+**
- [`uxplay`](https://github.com/FDH2/UxPlay) and its GStreamer plugins
- The **Discord desktop app** running on the same machine

## Install

### 1. Install uxplay + GStreamer

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt install uxplay avahi-daemon \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav

# Fedora
sudo dnf install uxplay gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad gstreamer1-libav

# Arch
sudo pacman -S uxplay gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav
```

If sound doesn't work afterwards, also install the audio plugin matching your setup (`gstreamer1.0-pipewire`, `gstreamer1.0-pulseaudio`, or `gstreamer1.0-alsa` on Debian-based systems).

### 2. Firewall (important!)

AirPlay needs several ports open, plus mDNS discovery — if the server doesn't show up on your phone, this is almost always the reason.

| Purpose | Protocol | Ports |
|---|---|---|
| mDNS / service discovery | UDP | 5353 |
| AirPlay defaults (bare `-p`) | TCP | 7100, 7000, 7001 |
| AirPlay defaults (bare `-p`) | UDP | 6000, 6001, 7011 |

```bash
# ufw
sudo ufw allow 5353/udp
sudo ufw allow 7100,7000,7001/tcp
sudo ufw allow 6000,6001,7011/udp

# firewalld
sudo firewall-cmd --permanent \
  --add-port=5353/udp \
  --add-port=7100/tcp --add-port=7000/tcp --add-port=7001/tcp \
  --add-port=6000/udp --add-port=6001/udp --add-port=7011/udp
sudo firewall-cmd --reload
```

Using a custom port with `--port N`? Open **TCP and UDP N, N+1, N+2** instead (e.g. `--port 35000` → 35000–35002).

### 3. Get AirPlayPresence

```bash
git clone https://github.com/exoplayerjib/AirplayPresence.git
cd AirPlayPresence
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. Create the Discord application (one-time)

1. Open <https://discord.com/developers/applications> → **New Application**.
2. Pick a name (it appears as a fallback when the first presence line is empty).
3. Copy the **Application ID** from *General Information*.

No bot, no OAuth, no token needed.

## Run

```bash
.venv/bin/python airplay_presence.py --client-id YOUR_APPLICATION_ID
```

The Now-Playing page opens in your browser automatically. On your phone:

1. Open **Apple Music** (or any audio app) and start playing.
2. Tap the **AirPlay** icon → select your AirPlayPresence server (default name `uxplay`, change with `--name`).

Discord updates within a couple of seconds — cover art, track, artist, album, and a live countdown. Pause or disconnect and the presence clears; resume and it comes back with the right timer.

Useful without Discord too: `--dry-run` runs everything except the Discord connection (no client ID required).

## Configuration

### Web UI

The page that opens on launch (`http://127.0.0.1:8080`) has a **"Discord presence lines"** panel:

- **First line / Second line / Third line / Fourth line** — what goes on each Discord presence line: Title, Artist, Album, Album artist, Genre, a fixed "Apple Music" text, your own custom text, or nothing.
- **Member list shows** — which line Discord uses next to your username.

Changes apply live and are saved to `~/.config/airplay-presence/config.json`.

### Command-line options

| Flag | Default | Description |
|---|---|---|
| `--client-id` / `$DISCORD_CLIENT_ID` | — | Discord application ID |
| `--port N` | legacy port set | AirPlay TCP port (uses N, N+1, N+2) |
| `--name NAME` | `uxplay` | AirPlay server name shown on devices |
| `--ui-port N` | 8080 | Local web UI port |
| `--no-ui` / `--no-browser` | — | Disable the web UI / auto-open |
| `--no-mpris` | — | Disable the D-Bus media player |
| `--no-image` | — | Disable cover-art upload |
| `--itunes-art` | — | Look up cover art on iTunes before uploading (upload-only by default) |
| `--meta-file` / `--image-file` | `/tmp/uxplay/…` | uxplay output file paths |
| `--config PATH` | `~/.config/airplay-presence/config.json` | Presence-lines config |
| `--min-interval S` | 5 | Minimum seconds between Discord updates; the presence is also re-sent at this cadence even when unchanged |
| `--debounce S` | 1.5 | Metadata file event debounce; rapid track changes within this window collapse into one update |
| `--dry-run` | — | Log payloads instead of talking to Discord |
| `--verbose` | — | Debug logging (includes all uxplay output) |
| `--no-uxplay` | — | Attach to an already-running uxplay instead of spawning one |

## Run at login (systemd user service)

Create `~/.config/systemd/user/airplaypresence.service`:

```ini
[Unit]
Description=AirPlayPresence (AirPlay to Discord Rich Presence)
After=graphical-session.target

[Service]
ExecStart=%h/AirPlayPresence/.venv/bin/python %h/AirPlayPresence/airplay_presence.py --client-id YOUR_APPLICATION_ID
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

(Adjust the paths if you cloned elsewhere.)

```bash
systemctl --user daemon-reload
systemctl --user enable --now airplaypresence.service
journalctl --user -u airplaypresence.service -f
```

## Troubleshooting

- **Server doesn't appear on the phone** — check the firewall section above; verify mDNS is running (`systemctl status avahi-daemon`).
- **Connects but no sound** — missing GStreamer audio plugins; install `gstreamer1.0-libav` and the plugin for your audio server (PipeWire/Pulse/ALSA).
- **No countdown timer in Discord** — progress data only exists for **audio-only (ALAC)** streaming, which is how Apple Music streams by default. Screen-mirroring (AAC) provides none — the presence still shows track info, just without times.
- **Cover art missing** — a valid cover file is uploaded to litterbox.catbox.moe (1-hour links; once a link expires, the next update uploads a fresh one), which needs internet access; pass `--itunes-art` to try the iTunes CDN first. Invalid/placeholder images are never uploaded. If Discord's first fetch of a link fails, the link is varied on every update so Discord re-fetches instead of pinning a broken placeholder.
- **Presence stops updating** — the Discord desktop app must be running on the same machine; sends are rate-limited to stay within Discord's limits (deferred sends are logged).

## Limitations

- **Playback control is disabled on purpose**: AirPlay receivers cannot command the client — your phone is the only remote. MPRIS advertises this honestly (`CanControl = false`).
- Elapsed/remaining times require audio-only (ALAC) streaming; screen-mirroring audio (AAC) provides no progress data.
- Discord Rich Presence only works with the desktop app on the same machine as the script.
