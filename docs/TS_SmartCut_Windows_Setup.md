# TS_SmartCut Windows Setup

English | [日本語](TS_SmartCut_Windows_Setup_jp.md)

For: preparing a Windows PC to run TS_SmartCut from source (an environment for using the app, not for developing it).
To build and distribute an .exe, see "2. Windows — building the .exe with PyInstaller" in
[TS_SmartCut_Packaging.md](TS_SmartCut_Packaging.md)
(the .exe needs no Python or virtual environment; install only items 2 and 3 of section 1 below — ffmpeg and mpv).

============================================================
## 1. What you need first
============================================================
1. **Python 3.10 or later** (the official installer, with "Add python.exe to PATH" turned on; checked with 3.14.7)
2. **ffmpeg / ffprobe** (6 or later. Extract an official build and add the folder holding `ffmpeg.exe`/`ffprobe.exe` to
   PATH; Chocolatey and similar also work)
3. **mpv and libmpv (`libmpv-2.dll`)** — install them as in "1.1 Installing mpv" below.
   The app looks for `libmpv-2.dll` (or `mpv-2.dll`) on PATH (and next to the executable) and loads it, so
   **`mpv.exe` alone is not enough**. The Chocolatey and winget (`mpv-player.mpv-CI.MSVC`) packages of mpv
   don't include this DLL, and loading it failed with OSError (2026-08, on a real PC; then with python-mpv's `import mpv`).
4. **Git for Windows** (to get and update the source)

### 1.1 Installing mpv (shinchiro's build)

From shinchiro's Windows builds (the Releases of `shinchiro/mpv-winbuild-cmake` on GitHub), take
**the two 7z files with the same date**.

| File | Contents | What is used |
|---|---|---|
| `mpv-x86_64-v3-<date>-git-<hash>.7z` | The player | `mpv.exe`, `mpv.com`, `d3dcompiler_43.dll`, etc. |
| `mpv-dev-x86_64-v3-<date>-git-<hash>.7z` | Development files (libmpv) | **`libmpv-2.dll`** (the library the app loads) |

- `x86_64-v3` is for CPUs with AVX2 (Intel 4th generation Haswell or later, AMD Zen or later). On older CPUs use
  `mpv-x86_64-…` and `mpv-dev-x86_64-…` without `-v3`.
- Extract both 7z files into **the same folder** (e.g. `C:\Tools\mpv\`) and add that folder to PATH.
  Extracting over winget's mpv folder also works, but updating mpv with winget can overwrite it and remove the DLL,
  so a folder of your own is better (build used for the check: v0.41.0-920-gdd5d17d32, 2026-08-09).
- To check (in a new PowerShell, after PATH has been reloaded):

  ```powershell
  where.exe libmpv-2.dll     # shows the libmpv-2.dll in the folder you extracted
  mpv --version              # shows the version of the build you extracted
  ```

============================================================
## 2. Getting the source
============================================================
Clone the GitHub repository (`github.com/babaken/TS_SmartCut`, branch `main`):

```powershell
git clone https://github.com/babaken/TS_SmartCut.git
cd TS_SmartCut
```

============================================================
## 3. Virtual environment and packages (PowerShell)
============================================================
```powershell
# Create the virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# Packages (PySide6. libmpv is loaded by the app's own binding (python-mpv is not needed since 2026-11-15). The auto-cut
# calculations and training are done by the Rust part (LightGBM and numpy are not needed since 2026-10-30; only to run
# the tests and the checking tools: `pip install numpy pytest`))
pip install -r requirements.txt

# Check
python -c "import PySide6; print('imports OK')"
ffmpeg -version
ffprobe -version
```

The Rust part of the auto-cut calculations (`native/`. **Required** since 2026-10-28. Without it the auto cut (features, proposals,
automatic tuning, training, logos) and the audio waveform don't work — an E506 notice appears after start. Cut editing and
conversion still work): install Rust (`winget install Rustlang.Rustup` or https://rustup.rs/ ; the C linker needs "Desktop
development with C++" from Visual Studio Build Tools), then in the virtual environment

```powershell
pip install .\native
python -c "import ts_smartcut_native; print(ts_smartcut_native.VERSION)"
```

Compared with when the calculations were in Python, features are about 2.5× faster and the automatic threshold tuning after
a batch about 6× faster (both measured on Windows). The Python versions were removed on 2026-10-28, so the app can't be used
without it. Reinstall with the same command whenever `native/` changes (including after a `git pull` that changes it).

============================================================
## 4. Starting the app
============================================================
Normally start it with `scripts\run_ts_smartcut.bat` (double-click it in Explorer; a shortcut on the desktop or in the
Start menu also works).

- It starts with the project's `.venv` `pythonw.exe`, so there is no need to activate the virtual environment, and no black
  console window appears.
- Wherever it is started from, it moves the working folder to the project root first.
- If the app doesn't start, run `scripts\run_ts_smartcut.bat /console` to see the error in a console.

Settings (`settings.json`), the auto-cut history DB (`cut_history.db`), logs (`logs\`), temporary files (`Temp\`),
the keyframe cache (`KeyframeCache\`) and station logos (`Logo\`) are created in the project root.
In a folder that can't be written to, the app doesn't start (E505).

============================================================
## 5. Updating
============================================================
In PowerShell:

```powershell
cd TS_SmartCut
.venv\Scripts\Activate.ps1
git pull
pip install -r requirements.txt   # in case the packages changed
pip install .\native              # when git pull listed native/ (or when the check below says it doesn't match)
```

Rebuilding `native\` takes a few minutes. A running app keeps using the old part, so restart it afterwards.

### Checking

```powershell
python -c "from ts_smartcut.autocut.native import native_problem as p; print(p() or 'native OK')"
ffmpeg -version                    # 6 or later
```

- `native OK` means the Rust part matches the app. If it doesn't, the installed and the required versions are shown;
  reinstall with `pip install .\native`.
- Restart the app and check that no E506 notice appears after start, and that "Help → System Info" shows the versions
  of ffmpeg, mpv and `ts_smartcut_native`.
- Opening a recording and seeing auto-cut proposals, and converting a short range in the batch, checks everyday use.
- To check the whole app, run the tests (they use the real ffmpeg, mpv and Qt; about 10 minutes):

```powershell
pip install -e ".[dev]"
python -m pytest -q -p no:faulthandler
```

============================================================
## 6. Troubleshooting
============================================================
- **No preview ("mpv: unavailable (preview-limited mode)" is shown)**: first check `where.exe libmpv-2.dll`. The app starts
  without mpv, and cut editing works.
- **The app doesn't start**: see the error with `scripts\run_ts_smartcut.bat /console`. The log is `logs\ts_smartcut.log`.
- **A rare crash on exit**: on Windows the app can rarely (a few percent) crash on exit (thought to be on the libmpv side).
  It happens only on exit, never while editing or encoding.
