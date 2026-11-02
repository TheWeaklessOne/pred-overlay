"""Updates from GitHub Releases: check, download, verify, swap the folder.

The latest release carries ``PredOverlay-vX.Y.Z-win64.zip`` and
``SHA256SUMS.txt``. A newer zip is downloaded, checked against the published
hash and unpacked next to the program folder. The app then quits and a short
Windows PowerShell script (signed by Microsoft, so Smart App Control lets it
run) swaps the folders and starts the new version; on a failure it puts the old
folder back. Settings and the session live elsewhere and stay. Network calls go
to api.github.com and GitHub's download hosts only.
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .config import home, log_dir

REPO = "TheWeaklessOne/pred-overlay"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY = 6 * 60 * 60
FIRST_CHECK = 60  # seconds after start: let sign-in and the first panel go first
MAX_ZIP = 300 * 1024 * 1024
TIMEOUT = 30
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP: outlives this process
BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB


def parse_version(text):
    """'v0.1.3' -> (0, 1, 3); None for anything else."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(n) for n in match.groups()) if match else None


def installable():
    """Only the built app can replace itself; a source checkout updates with git."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def app_dir():
    return Path(sys.executable).resolve().parent


def staging_dir(app=None):
    """Next to the program folder: the swap is then a rename on the same drive."""
    app = app or app_dir()
    return app.with_name(app.name + ".update")


def _get(url, limit):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"pred-overlay/{__version__}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if not response.geturl().startswith("https://"):
            raise OSError("update download left HTTPS")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise OSError("update file is larger than expected")
    return data


def latest_release():
    """{"version", "tag", "zip_name", "zip_url", "sums_url"} of the latest release."""
    found = json.loads(_get(LATEST, 1_000_000))
    version = parse_version(found.get("tag_name"))
    if version is None:
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in found.get("assets", [])}
    name = f"PredOverlay-{found['tag_name']}-win64.zip"
    if not assets.get(name) or not assets.get("SHA256SUMS.txt"):
        return None
    return {
        "version": version,
        "tag": found["tag_name"],
        "zip_name": name,
        "zip_url": assets[name],
        "sums_url": assets["SHA256SUMS.txt"],
    }


def expected_hash(sums_text, name):
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


APP_FILES = {"PredOverlay.exe", "PredOverlayCLI.exe", "_internal"}
STARTED = "started"  # the marker a freshly updated app writes, see mark_started()


def looks_like_app(folder):
    """True for a folder holding exactly this app's build (and nothing of the user's)."""
    try:
        names = {entry.name for entry in Path(folder).iterdir()}
    except OSError:
        return False
    return {"PredOverlay.exe", "_internal"} <= names and names <= APP_FILES


def unpack(data, target):
    """Unpack the release zip's ``PredOverlay/`` folder into ``target``; returns its path."""
    if target.exists():
        if not looks_like_app(target / "PredOverlay"):
            raise OSError(f"{target} exists and is not an earlier update; leaving it alone")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    root = target.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            path = (root / member.filename).resolve()
            if root not in path.parents and path != root:
                raise OSError("the update archive points outside its folder")
        archive.extractall(root)
    folder = root / "PredOverlay"
    if not looks_like_app(folder):
        raise OSError("the update archive does not hold the app")
    return folder


def download(release, app=None):
    """The unpacked, verified new program folder; raises OSError on a bad download."""
    app = app or app_dir()
    if not looks_like_app(app):
        raise OSError(f"{app} holds other files too; update it by hand from the releases page")
    sums = _get(release["sums_url"], 100_000).decode("utf-8", "replace")
    want = expected_hash(sums, release["zip_name"])
    if not want:
        raise OSError("the release lists no checksum for its zip")
    data = _get(release["zip_url"], MAX_ZIP)
    if hashlib.sha256(data).hexdigest() != want:
        raise OSError("the downloaded zip does not match its checksum")
    return unpack(data, staging_dir(app))


# Paths come in through environment variables, so no quoting can break the script.
# The new version proves it started by writing the marker; without it in 20 s the
# old folder goes back and starts with --update-failed (no automatic retry).
SWAP_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $env:TEMP   # never hold the program folder open ourselves
$app = $env:PO_APP; $new = $env:PO_NEW; $log = $env:PO_LOG; $marker = $env:PO_MARKER
$exe = $env:PO_EXE; $tag = $env:PO_TAG
$old = "$app.old"; $failed = "$app.failed"; $swapped = $false
function Note($text) { "$(Get-Date -Format s) $text" | Out-File -LiteralPath $log -Append -Encoding utf8 }
function Retry([scriptblock]$step) {
  for ($i = 0; ; $i++) {
    try { & $step; return } catch { if ($i -ge 20) { throw }; Start-Sleep -Milliseconds 500 }
  }
}
try {
  Wait-Process -Id ([int]$env:PO_PID) -Timeout 60 -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
  if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }
  Retry { Move-Item -LiteralPath $app -Destination $old }
  try { Retry { Move-Item -LiteralPath $new -Destination $app } }
  catch { Retry { Move-Item -LiteralPath $old -Destination $app }; throw }
  $swapped = $true
  Start-Process -FilePath (Join-Path $app $exe) -ArgumentList '--updated' -WorkingDirectory $app
  $waits = if ($env:PO_WAIT) { [int]$env:PO_WAIT } else { 40 }   # half-seconds
  for ($i = 0; $i -lt $waits -and -not (Test-Path -LiteralPath $marker); $i++) { Start-Sleep -Milliseconds 500 }
  if (-not (Test-Path -LiteralPath $marker)) { throw "the new version did not start" }
  Note "update $tag applied"
  Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath (Split-Path $new) -Recurse -Force -ErrorAction SilentlyContinue
} catch {
  Note "update $tag failed: $_"
  if ($swapped -and (Test-Path -LiteralPath $old)) {
    try {
      if (Test-Path -LiteralPath $failed) { Remove-Item -LiteralPath $failed -Recurse -Force }
      Retry { Move-Item -LiteralPath $app -Destination $failed }
      Retry { Move-Item -LiteralPath $old -Destination $app }
      Remove-Item -LiteralPath $failed -Recurse -Force -ErrorAction SilentlyContinue
      Note "previous version restored"
    } catch { Note "restore failed: $_" }
  }
  if (Test-Path -LiteralPath (Join-Path $app $exe)) {
    Start-Process -FilePath (Join-Path $app $exe) -ArgumentList '--update-failed', $tag -WorkingDirectory $app
  }
}
"""


def started_marker():
    return home() / "updates" / STARTED


def mark_started():
    """Called by a version launched with --updated: tells the swap script it runs."""
    marker = started_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(__version__, encoding="utf-8")


def swap_environment(pid, app, new, tag, exe="PredOverlay.exe"):
    return dict(
        os.environ,
        PO_PID=str(int(pid)),
        PO_APP=str(app),
        PO_NEW=str(new),
        PO_LOG=str(log_dir() / "update.log"),
        PO_MARKER=str(started_marker()),
        PO_EXE=exe,
        PO_TAG=tag,
    )


def run_swap(environment, wait=False):
    """Run SWAP_SCRIPT in a hidden Windows PowerShell; detached unless ``wait``."""
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        SWAP_SCRIPT,
    ]
    cwd = os.environ.get("TEMP") or str(Path(environment["PO_APP"]).parent)
    if wait:
        return subprocess.run(command, env=environment, cwd=cwd, creationflags=NO_WINDOW)
    for flags in (NO_WINDOW | NEW_GROUP | BREAKAWAY, NO_WINDOW | NEW_GROUP):
        try:  # leave any job object this app runs in, so the script outlives it
            return subprocess.Popen(
                command, env=environment, cwd=cwd, creationflags=flags, close_fds=True
            )
        except OSError:
            if not flags & BREAKAWAY:
                raise
    return None


def apply(new, tag):
    """Start the swap; the caller quits right after so the folder can move."""
    log_dir().mkdir(parents=True, exist_ok=True)
    run_swap(swap_environment(os.getpid(), app_dir(), new, tag))


def cleanup():
    """Leftovers of an earlier update, only where they are this app's own folders."""
    if not installable():
        return
    app = app_dir()
    for leftover in (app.with_name(app.name + ".old"), app.with_name(app.name + ".failed")):
        if looks_like_app(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
    staged = staging_dir(app)
    if looks_like_app(staged / "PredOverlay"):
        shutil.rmtree(staged, ignore_errors=True)


class Updater:
    """Background checks and downloads; the tray reads ``state`` on its timer.

    state: "idle", "checking", "current", "available" (newer, not downloaded),
    "downloading", "ready" (verified new folder on disk) or "error".
    """

    def __init__(self, log, clock=time.time, current=__version__, skip=None):
        self.log = log
        self.skip = skip  # a tag whose install failed: offered, never installed by itself
        self.clock = clock
        self.current = parse_version(current)
        self.state = "idle"
        self.release = None
        self.staged = None
        self.error = None
        self.next_check = clock() + FIRST_CHECK
        self.lock = threading.Lock()

    @property
    def busy(self):
        return self.state in ("checking", "downloading")

    def due(self):
        return not self.busy and self.state != "ready" and self.clock() >= self.next_check

    def check(self, download_too):
        """Ask GitHub for the latest release in the background."""
        with self.lock:
            if self.busy:
                return
            self.state, self.next_check = "checking", self.clock() + CHECK_EVERY
        threading.Thread(
            target=self._check, args=(download_too,), daemon=True, name="pred-update"
        ).start()

    def _check(self, download_too):
        try:
            release = latest_release()
        except Exception as exc:  # offline, rate limit: try again at the next check
            self._fail(f"update check failed: {type(exc).__name__}: {exc}")
            return
        if release is None or release["version"] <= self.current:
            self.state = "current"
            return
        self.release = release
        self.log(f"update available: {release['tag']}")
        if download_too and installable() and release["tag"] != self.skip:
            self._download()
        else:
            self.state = "available"

    def fetch(self):
        """Download the found release in the background."""
        with self.lock:
            if self.busy or not self.release:
                return
            self.state = "downloading"
        threading.Thread(target=self._download, daemon=True, name="pred-update").start()

    def _download(self):
        self.state = "downloading"
        try:
            self.staged = download(self.release)
        except Exception as exc:
            self._fail(f"update download failed: {type(exc).__name__}: {exc}")
            return
        self.log(f"update {self.release['tag']} downloaded and verified")
        self.state = "ready"

    def give_up(self, message):
        """Installing failed: stop retrying this release by itself."""
        self.skip = self.release["tag"] if self.release else self.skip
        self.error = message
        self.log(message)
        self.state = "error"

    def _fail(self, message):
        self.error = message
        self.log(message)
        self.state = "available" if self.release else "error"
