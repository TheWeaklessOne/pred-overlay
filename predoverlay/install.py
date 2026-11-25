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
from .config import windows_powershell

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
            windows_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        # PowerShell writes in the console's code page, which is not the one Python
        # would decode with: a byte it lacks must not cost the answer (or the start).
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
        env=dict(os.environ, **variables),
    )
    return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()


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
    """Once per install: shortcuts and autostart. Versions before 0.1.3 never
    turned autostart on, so it is switched on here too, unless the user disabled it
    in Task Manager. Returns what was set up, for a notice."""
    if not supported() or settings.get("integrated"):
        return []
    settings["integrated"] = True
    if _start_menu_shortcut().exists():
        # Set up before, and the settings that said so were lost: the user's choices
        # since (autostart switched off, a shortcut deleted) stand.
        return []
    done = []
    try:  # the tray is being built: nothing here may stop the app from starting
        ok, output = create_shortcuts()
        if ok:
            done.append("shortcuts")
        else:
            log(f"shortcuts not created: {output[:300]}")
    except Exception as exc:
        log(f"shortcuts not created: {type(exc).__name__}: {exc}")
    try:
        if not autostart.enabled() and not autostart.disabled_in_task_manager():
            autostart.set_enabled(True)
            done.append("autostart")
    except Exception as exc:
        log(f"autostart not enabled: {type(exc).__name__}: {exc}")
    return done


def _start_menu_shortcut():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT


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
