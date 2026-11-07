"""Hero portraits from Pred.gg's public assets, kept on disk.

A file is named after the hero's ``icon`` in the catalog, so new art under the same
hero is a new file. The panel draws a neutral tile until the file is here; nothing
waits for a download.
"""

import http.client
import os
import re
import time
import urllib.request

from . import __version__
from .config import portrait_dir

URL = "https://pred.gg/assets/{icon}_128.webp"
TIMEOUT = 8
PAUSE = 0.15  # seconds between downloads: a first start fetches every hero once
MAX_BYTES = 512 * 1024
_ICON = re.compile(r"[0-9A-Za-z_-]{1,64}")


def path(icon, directory=None):
    return (directory or portrait_dir()) / f"{icon}.webp"


def is_webp(body):
    """A whole WebP file: its RIFF header says how long it should be."""
    if len(body) <= 12 or body[:4] != b"RIFF" or body[8:12] != b"WEBP":
        return False
    return int.from_bytes(body[4:8], "little") + 8 <= len(body)


def _download(icon):
    request = urllib.request.Request(
        URL.format(icon=icon), headers={"User-Agent": f"pred-overlay/{__version__}"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
        return reply.read(MAX_BYTES + 1)


class PortraitIO:
    """Fetches the portraits missing from disk, one at a time, on its own lane."""

    def __init__(self, directory=None, download=_download, sleep=time.sleep):
        self.directory = directory
        self.download = download
        self.sleep = sleep

    def _store(self, icon):
        body = self.download(icon)
        if len(body) > MAX_BYTES or not is_webp(body):
            raise ValueError("not a portrait")
        target = path(icon, self.directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, target)  # the panel never reads half a file

    def handle(self, command):
        stored, failed, error = 0, [], None
        for icon in command[1]:
            if not _ICON.fullmatch(icon) or path(icon, self.directory).exists():
                continue
            try:
                self._store(icon)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                failed.append(icon)
                error = f"{type(exc).__name__}: {exc}"
            else:
                stored += 1
            self.sleep(PAUSE)
        events = [("portraits", stored, tuple(failed))]
        if failed:
            events.insert(0, ("note", f"{len(failed)} portraits not downloaded: {error}"))
        return events
