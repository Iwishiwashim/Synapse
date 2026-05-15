"""
Build standalone installer executables with PyInstaller.

Windows:  python installer/build.py          → dist/SynapseInstaller.exe
macOS:    python installer/build.py          → dist/SynapseInstaller.app

Requires: pip install pyinstaller
"""

import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIZARD = ROOT / "installer" / "setup_wizard.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ICON_WIN = ROOT / "installer" / "icon.ico"
ICON_MAC = ROOT / "installer" / "icon.icns"


def clean():
    for d in (DIST, BUILD):
        if d.exists():
            shutil.rmtree(d)
    for spec in ROOT.glob("SynapseInstaller*.spec"):
        spec.unlink()


def build():
    clean()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--name",
        "SynapseInstaller",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
    ]

    if sys.platform == "win32" and ICON_WIN.exists():
        cmd += ["--icon", str(ICON_WIN)]
    elif sys.platform == "darwin" and ICON_MAC.exists():
        cmd += ["--icon", str(ICON_MAC)]

    # Bundle the entire Synapse repo alongside the wizard so the installer
    # can find requirements.txt, server/, setup.py etc. at runtime.
    # The wizard uses Path(__file__).parent.parent to find ROOT — when frozen,
    # sys._MEIPASS is used instead, so we add the repo as a data directory.
    cmd += [
        "--add-data",
        f"{ROOT}{';' if sys.platform == 'win32' else ':'}synapse_src",
    ]

    cmd.append(str(WIZARD))

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\nBuild failed.")
        sys.exit(result.returncode)

    exe = DIST / ("SynapseInstaller.exe" if sys.platform == "win32" else "SynapseInstaller")
    app = DIST / "SynapseInstaller.app"
    artifact = app if app.exists() else exe
    print(f"\nDone: {artifact}")


if __name__ == "__main__":
    build()
