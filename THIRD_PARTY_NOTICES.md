# Third-party notices

TS_SmartCut itself is under the MIT License (`LICENSE`). The release files (the Windows installer and the
Linux archive) also contain the following software, each under its own license.

| Software | Used for | License |
|---|---|---|
| [Python](https://www.python.org/) (runtime and standard library) | the app runs on it | Python Software Foundation License |
| [Qt 6](https://www.qt.io/) and [Qt for Python (PySide6, Shiboken6)](https://doc.qt.io/qtforpython-6/) | the user interface | GNU LGPL v3 |
| [PyO3](https://github.com/PyO3/pyo3) and the Rust crates it uses (`libc`, `once_cell`, `portable-atomic`) | the Rust part (`ts_smartcut_native`) | MIT or Apache-2.0 |
| [PyInstaller](https://pyinstaller.org/) bootloader | starting the bundled app | GPL v2 with the bootloader exception |

## Qt / PySide6 (LGPL v3)

Qt and PySide6 are used unchanged, as the shared libraries PyInstaller collected into `_internal/`. You may
replace them with other builds of the same version; their source code is available from the Qt Project
(<https://download.qt.io/>, <https://code.qt.io/>). The full license text: <https://www.gnu.org/licenses/lgpl-3.0.html>.

## Programme information and ARIB character coding (rplsTOOL, rplsinfo)

Reading the programme information (SDT / EIT / TOT) and the ARIB STD-B24 character coding is based on
[rplsTOOL](https://github.com/tsukumijima/rplsTOOL), through this project author's own C# port of it. Its author
states that the source code may be used, in part or in whole, without restriction (「ソースコード(一部分もしくは
全体)の使用に制限はありません。」, `ソースファイルについて.txt`).

The ARIB STD-B24 character tables in `native/src/arib_tables.rs` are generated (by `tools/convert_arib_tables.py`)
from `characterSets.cpp` of [rplsinfo](https://github.com/tsukumijima/rplsinfo). Its author states that the
source files may be used, in part or in whole, without restriction (「ソースファイル（一部分、あるいは全体）の
使用に制限はありません。」, `ソースファイルについて.txt`). Thanks to the author of rplsTOOL and rplsinfo.

## Not bundled

ffmpeg / ffprobe and mpv (libmpv) are **not** included. Install them yourself (see the README); they come under
their own licenses (LGPL or GPL, depending on the build).
