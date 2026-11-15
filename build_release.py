"""Builds the release files: PyInstaller (ts_smartcut.spec) + an installer or archive, each with a .sha256.

    python build_release.py [--no-installer]

- Windows: dist/TS_SmartCut-<version>-setup.exe (Inno Setup, packaging/windows/TS_SmartCut.iss). ISCC.exe is
  taken from TS_SMARTCUT_ISCC, %LOCALAPPDATA%\\Programs\\Inno Setup 6 or %ProgramFiles(x86)%\\Inno Setup 6.
  --no-installer stops after dist/TS_SmartCut (the PyInstaller output).
- Linux: dist/TS_SmartCut-<version>-linux64.tar.gz (app/, share/, optional/, install.sh, uninstall.sh;
  packaging/linux/). optional/ holds Qt's GTK3 look, which install.sh adds only on PCs with GTK3.

The version is ts_smartcut.__version__. Run it with the venv that has PySide6, pyinstaller and
ts_smartcut_native installed (docs/TS_SmartCut_Packaging.md).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Mapping, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ts_smartcut import __version__  # noqa: E402

PACKAGING = ROOT / "packaging"
# Documents put next to the executable (missing ones stop the build)
DOCUMENTS = ("README.md", "README_jp.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
LINUX_SHARE = ("ts_smartcut.desktop",)
EXECUTABLE = "TS_SmartCut.exe" if platform.system() == "Windows" else "TS_SmartCut"


def pyinstaller_build() -> Path:
    for folder in (ROOT / "build", ROOT / "dist"):
        if folder.exists():
            shutil.rmtree(folder)
    print("== PyInstaller ==", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "ts_smartcut.spec", "--noconfirm"], cwd=ROOT, check=True)
    app_dir = ROOT / "dist" / "TS_SmartCut"
    if not (app_dir / EXECUTABLE).exists():
        raise SystemExit(f"{app_dir / EXECUTABLE} was not built")
    for name in DOCUMENTS:
        source = ROOT / name
        if not source.exists():
            raise SystemExit(f"Document to bundle is missing: {source}")
        shutil.copy(source, app_dir / name)
    return app_dir


def find_iscc(environ: Mapping[str, str] = os.environ) -> Optional[Path]:
    candidates = [environ.get("TS_SMARTCUT_ISCC", "")]
    if environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    if environ.get("ProgramFiles(x86)"):
        candidates.append(str(Path(environ["ProgramFiles(x86)"]) / "Inno Setup 6" / "ISCC.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def windows_installer(app_dir: Path, version: str) -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup's ISCC.exe was not found (winget install JRSoftware.InnoSetup, "
                         "or set TS_SMARTCUT_ISCC)")
    out_dir = app_dir.parent
    print(f"== Inno Setup ({iscc}) ==", flush=True)
    subprocess.run([str(iscc), f"/DAppVersion={version}", f"/DSourceDir={app_dir}", f"/O{out_dir}",
                    str(PACKAGING / "windows" / "TS_SmartCut.iss")], check=True)
    installer = out_dir / f"TS_SmartCut-{version}-setup.exe"
    if not installer.exists():
        raise SystemExit(f"Installer was not made: {installer}")
    return installer


def linux_tarball(app_dir: Path, version: str) -> Path:
    top = f"TS_SmartCut-{version}-linux64"
    tar_path = app_dir.parent / f"{top}.tar.gz"
    linux = PACKAGING / "linux"

    def tidy(info: tarfile.TarInfo) -> tarfile.TarInfo:
        # No build machine's user name in the archive, and no group/other write permission
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode &= ~0o022
        return info

    print(f"== tar.gz: {tar_path.name} ==", flush=True)
    with tarfile.open(tar_path, "w:gz") as tar:
        for name in ("install.sh", "uninstall.sh"):
            info = tar.gettarinfo(str(linux / name), f"{top}/{name}")
            info.mode = 0o755
            with open(linux / name, "rb") as f:
                tar.addfile(tidy(info), f)
        for name in LINUX_SHARE:
            tar.add(str(linux / name), f"{top}/share/{name}", filter=tidy)
        # Qt's GTK3 look (left out by the spec so the build PC's GTK3 and ICU stay out); install.sh puts it in
        # only when the PC has GTK3, which it then uses
        gtk3 = gtk3_platform_theme()
        tar.add(str(gtk3), f"{top}/optional/{gtk3.name}", filter=tidy)
        for path in sorted(app_dir.rglob("*")):
            relative = path.relative_to(app_dir).as_posix()
            info = tar.gettarinfo(str(path), f"{top}/app/{relative}")
            if relative == EXECUTABLE:
                info.mode |= 0o755
            if path.is_file():
                with open(path, "rb") as f:
                    tar.addfile(tidy(info), f)
            else:
                tar.addfile(tidy(info))
    return tar_path


def gtk3_platform_theme() -> Path:
    import PySide6

    path = Path(PySide6.__file__).resolve().parent / "Qt" / "plugins" / "platformthemes" / "libqgtk3.so"
    if not path.is_file():
        raise SystemExit(f"Qt's GTK3 platform theme is missing: {path}")
    return path


def write_sha256(path: Path) -> Path:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    sha_path = path.with_name(path.name + ".sha256")
    sha_path.write_text(f"{digest.hexdigest()}  {path.name}\n", encoding="ascii")
    return sha_path


def build(version: str, installer: bool = True) -> Path:
    app_dir = pyinstaller_build()
    system = platform.system()
    if system == "Windows":
        if not installer:
            print(f"== Done (no installer): {app_dir} ==")
            return app_dir
        result = windows_installer(app_dir, version)
    elif system == "Linux":
        result = linux_tarball(app_dir, version)
    else:
        raise SystemExit(f"No release files for {system}")
    sha_path = write_sha256(result)
    print(f"== Done: {result} ({result.stat().st_size / (1024 * 1024):.1f} MB) ==")
    print(sha_path.read_text(encoding="ascii").strip())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-installer", action="store_true", help="Windows: stop after the PyInstaller output")
    args = parser.parse_args()
    build(__version__, installer=not args.no_installer)


if __name__ == "__main__":
    main()
