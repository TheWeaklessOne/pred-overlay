"""The Windows version resource for the exe, from ``predoverlay.__version__``."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version():
    text = (ROOT / "predoverlay" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def version_resource():
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    numbers = (tuple(int(n) for n in re.findall(r"\d+", version())[:3]) + (0, 0, 0, 0))[:4]
    text = version()
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("ProductName", "Pred Overlay"),
                            StringStruct("FileDescription", "Pred Overlay"),
                            StringStruct("FileVersion", text),
                            StringStruct("ProductVersion", text),
                            StringStruct("LegalCopyright", "MIT License"),
                            StringStruct("OriginalFilename", "PredOverlay.exe"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
