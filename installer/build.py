"""
Build SynapseInstaller.exe with PyInstaller.

    pip install pyinstaller
    python installer/build.py

Output: dist/SynapseInstaller.exe  (Windows)
        dist/SynapseInstaller.app  (macOS)
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIZARD = ROOT / "installer" / "setup_wizard.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def main():
    for d in (DIST, BUILD):
        if d.exists():
            shutil.rmtree(d)
    for spec in ROOT.glob("SynapseInstaller*.spec"):
        spec.unlink()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "SynapseInstaller",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        str(WIZARD),
    ]

    icon_win = ROOT / "installer" / "icon.ico"
    icon_mac = ROOT / "installer" / "icon.icns"
    if sys.platform == "win32" and icon_win.exists():
        cmd += ["--icon", str(icon_win)]
    elif sys.platform == "darwin" and icon_mac.exists():
        cmd += ["--icon", str(icon_mac)]

    print("Building…")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        sys.exit(result.returncode)

    artifact = DIST / ("SynapseInstaller.exe" if sys.platform == "win32" else "SynapseInstaller")
    app = DIST / "SynapseInstaller.app"
    print(f"\nDone: {app if app.exists() else artifact}")


if __name__ == "__main__":
    main()
