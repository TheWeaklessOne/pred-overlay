# PyInstaller build: a folder with PredOverlay.exe (tray app, no console) and
# PredOverlayCLI.exe (the same program with a console, for --check).
#
#     pyinstaller packaging/PredOverlay.spec --noconfirm
#
# One folder rather than one file: it starts fast (nothing to unpack on every
# launch) and antivirus heuristics flag self-extracting exes far more often.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "packaging"))
from version_info import version_resource  # noqa: E402

hidden = collect_submodules("winrt") + collect_submodules("predoverlay")
excluded = [
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtPdf",
    "PySide6.QtCharts",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtOpenGL",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=hidden,
    excludes=excluded,
    datas=[(str(ROOT / "predoverlay" / "assets"), "predoverlay/assets")],
)
drop = (
    "opengl32sw",
    "Qt6Quick",
    "Qt6Qml",
    "Qt6Pdf",
    "Qt6VirtualKeyboard",
    "qpdf",
    "qtvirtualkeyboard",
    "qtiff",
)
a.binaries = [b for b in a.binaries if not any(d in b[0] for d in drop)]
a.datas = [d for d in a.datas if "translations" not in d[0]]
pyz = PYZ(a.pure)
icon = str(ROOT / "predoverlay" / "assets" / "icon.ico")
version = version_resource()

tray = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PredOverlay",
    console=False,
    icon=icon,
    version=version,
)
cli = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PredOverlayCLI",
    console=True,
    icon=icon,
    version=version,
)
coll = COLLECT(tray, cli, a.binaries, a.datas, name="PredOverlay")
