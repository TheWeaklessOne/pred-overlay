"""Desktop integration: shortcuts, autostart, uninstall, the setup's Apps entry.

The app sets itself up on its first start, whether the zip or the setup
(``packaging/installer.iss``) put it there: a Start menu and a desktop shortcut
(made by Windows PowerShell, which is signed by Microsoft) and the autostart
entry. ``--uninstall`` takes them away again. Smart App Control may block an
unsigned setup exe, so the zip stays the way that needs no installer.
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


# The entry the setup makes in Windows' Apps list (AppId in packaging/installer.iss).
SETUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{2104A709-9A62-4DDB-B3CF-AF9AF36B84E8}_is1"


def sync_setup_entry(version, app=None, registry=None):
    """After a self-update the Apps entry still names the version the setup installed:
    bring it up to date. True when it was changed; nothing happens for a zip install
    or for an entry that points at another folder."""
    if registry is None:
        if not supported():
            return False
        import winreg as registry
    app = Path(app or app_exe().parent)
    access = registry.KEY_READ | registry.KEY_SET_VALUE
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, SETUP_KEY, 0, access) as key:
            location = registry.QueryValueEx(key, "InstallLocation")[0]
            if not _same_folder(location.rstrip("\\/"), app):
                return False
            if registry.QueryValueEx(key, "DisplayVersion")[0] == version:
                return False
            registry.SetValueEx(key, "DisplayVersion", 0, registry.REG_SZ, version)
            return True
    # No entry (a zip install), one without these values, or values of another type:
    # nothing here may keep the app from starting.
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def _same_folder(a, b):
    try:
        return os.path.samefile(a, b)  # also through 8.3 names and junctions
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(str(b)))


def uninstall(settings_path=None):
    """Remove shortcuts and autostart; the program and data folders are left to delete.

    The settings forget that the app set itself up, so an install that keeps them
    makes the shortcuts and autostart again on its first start."""
    lines = []
    from . import settings as settings_module

    values = settings_module.load(settings_path)
    if values["integrated"]:
        values["integrated"] = False
        try:
            settings_module.save(values, settings_path)
        except OSError as exc:
            lines.append(f"Settings not updated: {exc}")
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
