"""Desktop integration without an installer: shortcuts, autostart, uninstall.

Windows' Smart App Control blocks unsigned installers outright, while the app's
own exe runs. So the app sets itself up on its first start: a Start menu and a
desktop shortcut (made by Windows PowerShell, which is signed by Microsoft) and
the autostart entry. ``--uninstall`` takes them away again.
"""

import os
import subprocess
import sys
from pathlib import Path

from . import autostart

SHORTCUT = "Pred Overlay.lnk"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def app_exe():
    """The windowed exe of this build (also when running PredOverlayCLI.exe)."""
    return Path(sys.executable).with_name("PredOverlay.exe")


def supported():
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def powershell(script, timeout=20, **variables):
    """Run a short Windows PowerShell script with no window; returns (ok, output).

    ``variables`` reach the script as environment variables, so paths need no quoting.
    """
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
        env=dict(os.environ, **variables),
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def create_shortcuts(exe=None):
    exe = Path(exe or app_exe())
    script = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Programs'),
                      [Environment]::GetFolderPath('Desktop'))) {
  $link = $shell.CreateShortcut((Join-Path $folder $env:PO_LINK))
  $link.TargetPath = $env:PO_EXE
  $link.WorkingDirectory = $env:PO_DIR
  $link.IconLocation = "$env:PO_EXE,0"
  $link.Description = 'Pred Overlay'
  $link.Save()
}
"""
    return powershell(script, PO_LINK=SHORTCUT, PO_EXE=str(exe), PO_DIR=str(exe.parent))


def remove_shortcuts():
    script = """
foreach ($folder in @([Environment]::GetFolderPath('Programs'),
                      [Environment]::GetFolderPath('Desktop'))) {
  Remove-Item -LiteralPath (Join-Path $folder $env:PO_LINK) -ErrorAction SilentlyContinue
}
"""
    return powershell(script, PO_LINK=SHORTCUT)


def integrate(settings, log):
    """Once per install: shortcuts, and autostart on a fresh install (an earlier
    version's user may have switched it off). Returns what was set up, for a notice."""
    if not supported() or settings.get("integrated"):
        return []
    done = []
    try:
        ok, output = create_shortcuts()
        if ok:
            done.append("shortcuts")
        else:
            log(f"shortcuts not created: {output[:300]}")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"shortcuts not created: {exc}")
    if not settings.get("welcomed"):
        try:
            if not autostart.enabled():
                autostart.set_enabled(True)
            done.append("autostart")
        except OSError as exc:
            log(f"autostart not enabled: {exc}")
    settings["integrated"] = True
    return done


def uninstall():
    """Remove shortcuts and autostart; the program and data folders are left to delete."""
    lines = []
    if autostart.supported():
        try:
            autostart.set_enabled(False)
            lines.append("Autostart removed.")
        except OSError as exc:
            lines.append(f"Autostart not removed: {exc}")
        try:
            ok, output = remove_shortcuts()
            lines.append("Shortcuts removed." if ok else f"Shortcuts not removed: {output}")
        except (OSError, subprocess.SubprocessError) as exc:
            lines.append(f"Shortcuts not removed: {exc}")
    from .config import home

    if supported():
        lines.append(f"Now quit the tray app and delete {Path(sys.executable).parent}")
    lines.append(f"To remove settings and the session too, delete {home()}")
    return "\n".join(lines)
