"""Build QTsys desktop launcher as a Windows EXE.

The generated executable is placed at the project root as ``QTsys启动器.exe``
so users can double-click it without relying on the Windows .pyw file
association.

This script intentionally builds ``launcher/light_app.py`` instead of the full
Python launcher. The light launcher uses only the standard library and delegates
project-specific checks to the project's own Python interpreter at runtime. This
keeps the executable small and avoids bundling pandas/scipy/Qt/backend modules.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "launcher" / "light_app.py"
BUILD_NAME = "QTsys_Launcher"
EXE_NAME = "QTsys启动器"


def run() -> int:
    if not ENTRY.exists():
        print(f"Entry file not found: {ENTRY}", file=sys.stderr)
        return 1

    for folder in (ROOT / "build", ROOT / "dist"):
        if folder.exists():
            shutil.rmtree(folder)

    output_exe = ROOT / f"{EXE_NAME}.exe"
    if output_exe.exists():
        output_exe.unlink()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        BUILD_NAME,
        "--distpath",
        str(ROOT),
        "--workpath",
        str(ROOT / "build" / "launcher"),
        "--specpath",
        str(ROOT / "build" / "launcher"),
        str(ENTRY),
    ]
    print("Building lightweight QTsys launcher EXE...")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    built_exe = ROOT / f"{BUILD_NAME}.exe"
    if built_exe.exists() and built_exe != output_exe:
        built_exe.replace(output_exe)

    if not output_exe.exists():
        print(f"Build finished but EXE was not found: {output_exe}", file=sys.stderr)
        return 2

    print(f"Launcher EXE created: {output_exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
