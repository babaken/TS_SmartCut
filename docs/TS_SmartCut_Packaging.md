# TS_SmartCut Packaging

English | [日本語](TS_SmartCut_Packaging_jp.md)

For: distributing TS_SmartCut (after phases 0–10) as an application launcher shortcut on Linux and as a distributable
.exe on Windows.

The external tools (ffmpeg/ffprobe/mpv) are not bundled; they are installed separately where the app runs and put on
PATH (decided 2026-08-21). Bundling them would require including their license texts (ffmpeg/mpv are LGPL or GPL
depending on the build).

============================================================
## 1. Linux — desktop launcher
============================================================

These two files exist:

- `scripts/run_ts_smartcut.sh` — a wrapper that starts `ts_smartcut.main` with the Python in the project's `.venv`.
  It `cd`s to the project root explicitly before running `python -m ts_smartcut.main` (when started through
  `systemd-run --user --scope`, as KDE Plasma 6's Kickoff does, the working directory was not the project root and the
  start failed with `ModuleNotFoundError` on a real PC, so the script doesn't depend on it).
- `~/.local/share/applications/ts_smartcut.desktop` — the entry shown in the application menu (`Exec=` points to the
  wrapper above, `Path=` gives the project root, `Icon=` see the icon below).

To set it up again:
```bash
chmod +x scripts/run_ts_smartcut.sh
# ts_smartcut.desktop is not in the repository; create it on another PC (<repo> is the project's absolute path)
cat > ~/.local/share/applications/ts_smartcut.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=TS_SmartCut
Exec=<repo>/scripts/run_ts_smartcut.sh
Path=<repo>
Icon=<repo>/ts_smartcut/gui/resources/icon.png
Terminal=false
Categories=AudioVideo;Video;
EOF
update-desktop-database ~/.local/share/applications   # refresh the menu cache
kbuildsycoca6 --noincremental                          # refresh KDE's application list cache (on KDE)
```

### Icon (one file)

The icon is **only** `ts_smartcut/gui/resources/icon.png` (in the project, the one source kept in Git). Both places
below point to this same file (no copies):

- The `.desktop` file's `Icon=` points to it with an absolute path
  (`Icon=/path/to/TS_SmartCut/ts_smartcut/gui/resources/icon.png`)
  → shown in application lists such as Kickoff
- `app.setWindowIcon(QIcon(...))` in `ts_smartcut/main.py` loads the same file → the icon of the running window and the
  taskbar

To replace it, overwrite `ts_smartcut/gui/resources/icon.png` and both change (the `.desktop` file uses an absolute
path, so nothing has to move; KDE caches the application list's icons, so if it doesn't change, refresh the cache with
`kbuildsycoca6 --noincremental`).

Windows (2026-10-06):
- Taskbar: started with `pythonw.exe`, the taskbar showed Python's icon (Windows groups a process with the executable
  that started it). `main.py` calls `SetCurrentProcessExplicitAppUserModelID("babaken.TS_SmartCut")` before creating
  the window, so the icon from `setWindowIcon()` is shown (checked with a screenshot).
- .exe: `ts_smartcut/gui/resources/icon.ico` is given as `icon=` in `ts_smartcut.spec`. The `.ico` is made from
  `icon.png` by `tools/make_icon.py` (an ICO holding 16–256 px PNGs, no Pillow needed). After replacing `icon.png`, run
  `python tools/make_icon.py` again.

On Wayland the taskbar / window switcher icon doesn't follow the `.desktop` file's `Icon=` alone: the app itself has to
link itself to its `.desktop` file name (without the extension) with Qt's `app.setDesktopFileName()` (done in `main.py`,
after seeing on a real PC that the menu showed the icon while the running app's taskbar icon stayed the generic one).

Checked: running `scripts/run_ts_smartcut.sh` directly opens the window, the log (then
`~/.config/ts_smartcut/logs/ts_smartcut.log`; since 2026-10-12 `logs/ts_smartcut.log` in the project root) shows the
mpv connection (in English since 2026-10-13: `Connected to mpv`), and a screenshot shows the icon in the taskbar.

### Windows — start script (without an .exe)
`scripts/run_ts_smartcut.bat` — the Windows version of `run_ts_smartcut.sh`. It `cd`s to the project root and starts
`.venv\Scripts\pythonw.exe -m ts_smartcut.main` with `start` (no console window). With `/console` it starts
`python.exe`, shows errors in the console and `pause`s after exit. If there is no `.venv` it says so. cmd.exe reads .bat
files in the ANSI code page (cp932), so the file is ASCII only, with CRLF line ends (fixed by `.gitattributes`).
Checked (2026-10-05, Windows 11): run from another folder, the window opens and the log shows the start and the mpv
connection (now in English: `TS_SmartCut started`, `Connected to mpv`).

============================================================
## 2. Windows — building the .exe with PyInstaller
============================================================

### 2.1 Files used
`ts_smartcut.spec` (project root) — the build definition shared by Linux and Windows. It uses the `--onedir` form
(a folder; starts fast and is the form PySide6 recommends. `--onefile` unpacks to a temporary folder on every start,
which adds a few seconds, and is reported to be unstable with Qt apps, so it is not used).

### 2.2 Preparation (Windows, PowerShell)
The app must already run from source as in [TS_SmartCut_Windows_Setup.md](TS_SmartCut_Windows_Setup.md).
In addition:
```powershell
cd C:\path\to\TS_SmartCut
.venv\Scripts\Activate.ps1
pip install -e ".[build]"     # adds pyinstaller>=6.0
```

### 2.3 Building
```powershell
pyinstaller ts_smartcut.spec --noconfirm
```
Output: the `dist\TS_SmartCut\` folder (holding `TS_SmartCut.exe` and `_internal\`).
Distribute the whole folder as a ZIP.

Unused Qt parts are left out by `ts_smartcut.spec` (2026-10-28). The app uses only QtCore, QtGui, QtWidgets and
QtOpenGLWidgets; QML, Quick, the virtual keyboard, PDF, networking, TLS and Qt's translations, which PySide6's hooks pick
up, are not included (`_UNUSED_QT`). The folder went from about 239 MB to about 211 MB and from 360 to 243 files (with
the Rust part and LightGBM; the large remaining parts were PySide6 about 75 MB, scipy (used by LightGBM) about 67 MB and
numpy about 28 MB. On 2026-10-30 training moved to Rust and LightGBM and scipy were no longer used (design §31.9
"training in Rust (④)"); the same day numpy was dropped as well ("dropping numpy (⑤)"). The `excludes` in
`ts_smartcut.spec` leave out numpy, lightgbm and scipy (even when the build PC's virtual environment has numpy for the
tests). Measured builds: about 137 MB after training moved to Rust, about 108 MB after numpy was also left out
(2026-10-30; no numpy, scipy or lightgbm in `_internal\`; the large parts are PySide6 about 75 MB, python314.dll about
7 MB and libcrypto about 12 MB)). With the parts left out, the built program was checked for: start, connecting to mpv,
opening a recording with a picture in the preview, the settings window (6 tabs), System Info and the style (windows11).
When a new Qt part is used (an added `import PySide6.Qt…`), check that it isn't in `_UNUSED_QT`.

Also left out (2026-11-19): the Python modules QtOpenGL and QtSvg (importing `QtOpenGLWidgets` doesn't load QtOpenGL),
the software OpenGL (`opengl32sw.dll`, 19.7 MB; GPU OpenGL worked even over Remote Desktop, so it was left out. On a PC
without GPU OpenGL the preview may not show a picture; editing still works), unused platform plugins (Windows keeps only
`qwindows`, Linux only `wayland` and `xcb`) and image formats (only ICO; PNG is read by Qt itself; Linux also keeps SVG
because icon themes use it). On Linux the bundled libraries are stripped (`strip`; `libpython` 33.6 → 21.1 MB). The Rust
part is stripped too (`strip` in `native/Cargo.toml`). The Windows folder went from 94 to 62 MB, the Linux tar.gz from
52.2 to 46.3 MB. With the parts left out, `tools/make_screenshots.py` was run with only the same Qt parts and checked:
a recording opens with a picture in the preview, and every screen and dialog appears.

OpenSSL is not included either (2026-11-15). The app doesn't communicate, so `excludes` leaves out `ssl`, `_ssl` and
`_hashlib`. `hashlib` uses Python's own sha1 and sha256 (the same values as with OpenSSL; used for the keyframe cache
names and the history hashes). This keeps Python's `libcrypto-3.dll` and `libssl-3.dll` out. The `libcrypto-3-x64.dll`
and `libssl-3-x64.dll` that Qt's TLS part asks for were picked up from other software on PATH (Git, for example) and
nothing in the .exe loaded them, so `_UNUSED_QT` leaves them out. The folder went from 107 MB to 94 MB and from 117 to
111 files (Python 3.14.10, PySide6 6.11.2). With them left out, start and connecting to mpv were checked.
Python 3.15 is not used for now (2026-11-15): PySide6 6.12.0, which works with 3.15, replaces `builtins.__import__`, so
3.15's lazy imports (`lazy from …`) are not replaced by the real objects and `concurrent.futures.ThreadPoolExecutor`
can't be called (the parallel automatic tuning fails; 2 tests in `tests/test_native.py`). When a new PySide6 comes out,
check again with the tests and the .exe.

### 2.4 Checking
```powershell
cd dist\TS_SmartCut
.\TS_SmartCut.exe
```
- The window opens; the menus and cut editing work
- A video opens and the preview actually draws (needs mpv-2.dll/libmpv-2.dll on PATH; if missing, add the folder of the
  build installed as in TS_SmartCut_Windows_Setup.md §1.1 to PATH)
- Settings, the history DB and logs are created in the `.exe`'s folder (`settings.json`, `cut_history.db`, `logs\`)
  (until 2026-10-12 `%APPDATA%\ts_smartcut\`; in a folder that can't be written to, such as Program Files, the app
  doesn't start)
- The Japanese and English menus both show without garbled text
  (that `_internal\ts_smartcut\i18n\languages\*.json` is bundled was checked beforehand on Linux; `language_manager.py`
  resolves this path from `sys._MEIPASS` when `sys.frozen`)
- Auto-cut training (A8, 2026-10-26; trained by the Rust part since 2026-10-30): "Retrain" works under Settings →
  "Auto Cut" → "Learning". `_internal\` holds no `lightgbm` or `scipy` (if it does, remove them from the build PC's
  virtual environment)
- The Rust part of the auto-cut calculations (`native/`, 2026-10-27; **required since 2026-10-28**): installed in the
  build's virtual environment with `pip install .\native`, `ts_smartcut_native` is bundled (no change to
  `ts_smartcut.spec` needed; the `.pyd` is in `_internal\ts_smartcut_native\`). Built without it, an E506 notice appears
  after start and the auto cut (features, proposals, automatic tuning, training, logos) and the audio waveform don't
  work. Check that System Info shows "ts_smartcut_native <version>" (11 since 0.1.3 on 2026-11-18), or that the automatic
  tuning log says "N threads". On 2026-10-28, with a check program frozen in the same environment as the .exe built with
  it, the features (about 10 s in Rust, then about 30 s in Python, for a 2.1 GB recording) and the automatic tuning (a
  copy of the history DB, 314 recordings) ran in Rust and gave the same values as Python

### 2.5 Checked beforehand on Linux (in that session)
A real Windows executable can't be built there (no cross-compiling), so `ts_smartcut.spec` was built and run once on
Linux and these were checked:
- `pyinstaller ts_smartcut.spec` completes without errors
- PySide6's hiddenimports (including QtOpenGL/QtOpenGLWidgets) are neither missing nor extra
- The i18n JSON files (`language_jp.json`/`language_en.json`) are bundled under `_internal/` with the right relative
  layout, and the frozen-path code (`ts_smartcut/i18n/language_manager.py`) finds them
- The built executable starts, logs the start and the mpv connection, and keeps running for 6 seconds without crashing
  (now in English: `TS_SmartCut started`, `Connected to mpv`)

Windows-only items to check (can't be checked on Linux):
- Whether `mpv-2.dll`/`libmpv-2.dll` is found through PATH
  (the app (`ts_smartcut/mpv/libmpv.py`; python-mpv until 2026-11-15) looks for this DLL through ctypes, so PyInstaller's
  static analysis doesn't find it and `binaries=[]` stays empty = not bundled. It is installed on PATH where the app runs,
  as in TS_SmartCut_Windows_Setup.md)
- Whether Windows Defender and similar flag the unsigned exe (PyInstaller executables are sometimes flagged; signing is
  not done for now)
- Whether `ffmpeg/gpu.py` still detects the GPU encoders (QSV/NVENC) in the .exe

### 2.6 Known open items
- No code signing.
- **There is no record of building the .exe on Windows and doing every check in 2.4** (as of 2026-10-17). On 2026-10-28
  it was checked that the .exe with the Rust part starts, creates the history DB next to the .exe and connects to mpv,
  plus the Rust part checks above. The parallel automatic threshold tuning uses threads only since 2026-10-28
  (`multiprocessing`'s spawn and `freeze_support()` in `main.py` were removed). Do 2.3 and 2.4 before distributing.
- The Linux PyInstaller output is also distributed since 2026-11-15 (3 below). Build and check the Linux release on a
  Linux PC.

## 3. Release files (installers) — `build_release.py` (2026-11-15)

After PyInstaller (`ts_smartcut.spec`), it makes an installer on Windows and a tar.gz on Linux, each with a SHA-256
`.sha256`. The version is `__version__` in `ts_smartcut/__init__.py`. README.md, README_jp.md, LICENSE and
THIRD_PARTY_NOTICES.md are placed next to the executable (it stops if one is missing).

```powershell
# In a virtual environment with PySide6, pyinstaller and ts_smartcut_native
python build_release.py            # Windows: dist\TS_SmartCut-<version>-setup.exe, Linux: dist/TS_SmartCut-<version>-linux64.tar.gz
python build_release.py --no-installer   # Windows: stops after dist\TS_SmartCut
```

- Install the tested PySide6 version in the build's virtual environment **from the start**
  (`pip install "PySide6==<version>"`). Replacing a newer version with an older one left parts missing and made an .exe
  that didn't start (`DLL load failed while importing QtGui`, 2026-11-15).
- **Windows** (`packaging/windows/TS_SmartCut.iss`, Inno Setup 6. ISCC.exe is looked for in `TS_SMARTCUT_ISCC`,
  `%LOCALAPPDATA%\Programs\Inno Setup 6` and `%ProgramFiles(x86)%\Inno Setup 6`, in that order)
  - Installs per user into `%LOCALAPPDATA%\Programs\TS_SmartCut` (no administrator rights needed). Settings, the history
    DB and logs are written next to the executable, so it doesn't install into Program Files (not writable; E505 stops
    the start).
  - An update replaces only the program parts (`_internal` as a whole) and keeps settings, the history DB, logs, `Logo\`
    and so on.
  - Uninstalling asks whether to also delete settings, history, logs, caches and `Logo\` (kept by default; a silent
    uninstall keeps them).
  - At the end it warns if ffmpeg, ffprobe or libmpv is not on PATH. The screens are in Japanese and English.
  - Checked (2026-11-15, Windows 11): silent install → starts and connects to mpv → update with the same version
    (settings and history DB kept, parts only in the old version removed) → silent uninstall (removed from the program
    folder, the Start menu and the app list; settings and history kept). The installer was 27.3 MB, the installed folder
    94 MB.
- **Linux** (`packaging/linux/`): the tar.gz holds `app/` (the PyInstaller output), `share/ts_smartcut.desktop`,
  `install.sh` and `uninstall.sh` (no owner names inside).
  - `sh install.sh` (no root needed) installs into `${XDG_DATA_HOME:-~/.local/share}/TS_SmartCut` and places
    `~/.local/share/applications/ts_smartcut.desktop`. Updating is the same command and replaces only `_internal` and the
    executable. At the end it warns if ffmpeg, ffprobe or libmpv (`ldconfig -p`) is missing.
  - `sh ~/.local/share/TS_SmartCut/uninstall.sh` uninstalls (asks whether to delete settings and history too; `--purge`
    deletes them without asking; when they are kept, uninstall.sh stays as well and `--purge` can delete them later).
  - Leave out the build PC's own parts (2026-11-15, found while checking on Linux):
    - libmpv: `libmpv.py` doesn't write the name directly in a `find_library()` call (written directly, PyInstaller
      bundled the build PC's libmpv with its ffmpeg, x264, x265, GTK and more; the tar.gz was 146.9 MB). The spec stops
      if libmpv gets in.
    - Unused Qt parts are left out in the Linux locations too (`PySide6/Qt/lib`, `plugins`, `translations`), and so are
      the VNC platform and the TUIO touch part (they need Qt Network). Of the input parts only the virtual keyboard is
      left out (ibus and compose are needed for Japanese input).
    - Parts are left out after the analysis, so libraries used only by the removed parts (OpenSSL, Kerberos, etc.)
      remain. The spec removes the libraries directly in `_internal` that the remaining parts don't reach through ldd
      dependencies (the removed names are printed in the build output).
    - Qt's GTK3 theme part (`platformthemes/libqgtk3.so`) pulls in the build PC's GTK3 and ICU (about 42 MB), so it is
      not bundled but placed in the tar.gz's `optional/`. `install.sh` installs it only when the PC has GTK3
      (`libgtk-3.so.0`), and it uses the PC's GTK3. The app works without it (only the look and the file dialog don't
      match GTK). Not checked on a GNOME PC yet.
    - The glib family (`libglib-2.0`, `libgobject-2.0`, `libgio-2.0`, `libgmodule-2.0`, `libgthread-2.0`) is not bundled;
      the PC's is always used. The PC's libmpv (gio, pango and so on through ffmpeg) and the GTK3 theme part load the PC's
      gobject and gio, so a bundled glib from the build PC would mix with a glib of another version.
    - For the same reason `libstdc++`, `libgcc_s`, `libfontconfig`, `libfreetype`, `libpcre2-8`, `libffi`, `libz`,
      `libexpat` and `libpng16` are not bundled (`_SYSTEM_LIBRARIES`). The PyInstaller executable looks in `_internal`
      first, so bundled older 24.04 versions were loaded before the PC's newer ones, and on Arch libmpv (the PC's
      libopenal needs a newer libstdc++) and the GTK3 theme (the PC's pango needs a newer fontconfig) couldn't load
      (2026-11-15).
    - Child processes (ffmpeg, ffprobe, the file manager of "Open Folder", etc.) don't search `_internal`: at the start of
      `main()` the `LD_LIBRARY_PATH` rewritten by PyInstaller is set back to `LD_LIBRARY_PATH_ORIG` (the value before the
      start) (`child_processes.restore_library_path_for_children`; the running app's own search path is fixed at start,
      so it doesn't change). Before that, on Arch ffmpeg's test encode failed with the bundled old libstdc++ and the
      hardware encoders came out as "none".
    - Likewise, PyInstaller's bootloader values (`_PYI_*`) are removed at the very start, and `QT_PLUGIN_PATH` and
      `QML2_IMPORT_PATH`, added by PySide6's runtime hook, are removed after the `QApplication` is created
      (`forget_bundled_qt_paths`; before removing them, `QCoreApplication.setLibraryPaths` fixes the app's own search
      path). Before that, Arch's Dolphin started by "Open Folder" (the PC's Qt 6.12) loaded the bundled Qt 6.11 parts
      (such as the Wayland part). The Windows .exe does the same (`_PYI_*`, the Qt values).
    - The menu entry is `ts_smartcut.desktop`. It must match the name the app gives with
      `setDesktopFileName("ts_smartcut")`, or the KDE Wayland taskbar shows a generic icon.
  - **The Linux release is built on Ubuntu 24.04 (WSL)** (decided 2026-11-15). A PyInstaller release only runs on the
    build PC's glibc or later. Built on a newer distribution (Arch, for example), it doesn't start on older ones. It is
    built on 24.04 and stated as "glibc 2.39 or later" (the README's requirements); the last run check is on a newer PC
    (Arch).
    - How: install Ubuntu 24.04 in WSL (`wsl --install -d Ubuntu-24.04 --location <a D drive folder, etc.> --no-launch`;
      the build can run as root), install build-essential, ffmpeg, libmpv2, the X / Wayland parts Qt uses and
      libgtk-3-0t64 with apt, install rustup and uv (Python 3.14 with `uv python install 3.14`), put `PySide6==6.11.2`,
      pyinstaller and `./native` into a `uv venv`, and run `build_release.py`. `install.sh` refuses root, so check it as
      an ordinary user.
    - `.sh` and `.desktop` files are always LF (`.gitattributes`). Built from a copy of a Windows working folder (checked
      out with CRLF), `install.sh` failed with `set: Illegal option -`.
    - Checked (2026-11-15, WSLg on Ubuntu 24.04): tar.gz 54.1 MB (165 entries, 146 MB unpacked), the highest glibc
      needed is 2.38, nothing missing for the parts in `_internal`. Install ("GTK3 found" installs the GTK3 theme part) →
      starts and connects to mpv with the PC's libmpv (0.37, API 2.2) → the GTK3 theme part loads with the PC's GTK3 and
      glib → update (settings and history kept, parts only in the old version removed) → uninstall ("N" keeps settings,
      history and uninstall.sh; `--purge` removes everything). Japanese input with ibus was not checked.
