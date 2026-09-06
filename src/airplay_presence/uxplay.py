from __future__ import annotations

import logging
import argparse
import collections
import subprocess
import threading
from pathlib import Path

from .constants import PROGRESS_RE
from .state import ProgressState


log = logging.getLogger("airplay-presence")

def spawn_uxplay(args: argparse.Namespace, meta_path: Path, image_path: Path) -> subprocess.Popen:
    command = ["uxplay"]
    if args.port is None:
        command.append("-p")
    else:
        command += ["-p", str(args.port)]
    if args.name:
        command += ["-n", args.name]
    command += ["-vs", "0", "-md", str(meta_path), "-ca", str(image_path)]
    log.info("starting: %s", " ".join(command))
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )


def read_uxplay_output(
    proc: subprocess.Popen,
    progress: ProgressState,
    tail: collections.deque,
    dead: threading.Event,
) -> None:
    stream = proc.stdout
    while True:
        line = stream.readline()
        if not line:
            break
        text = line.rstrip()
        tail.append(text)
        match = PROGRESS_RE.search(text)
        if match:
            position = int(match.group(1)) * 60 + int(match.group(2))
            remaining = int(match.group(3)) * 60 + int(match.group(4))
            duration = int(match.group(5)) * 60 + int(match.group(6))
            progress.set(float(position), float(duration))
            log.info("uxplay: %s", text)
        else:
            log.debug("uxplay: %s", text)
    dead.set()


def new_track_signature(snap: tuple[float, float], prior: tuple[float, float] | None) -> bool:
    if prior is None:
        return True
    return snap[1] != prior[1] or snap[0] <= prior[0]
