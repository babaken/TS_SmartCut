# -*- mode: python ; coding: utf-8 -*-
"""TS_SmartCut PyInstallerビルド定義（--onedir形式）。
Linux/Windows共通: `pyinstaller ts_smartcut.spec`で実行する。
出力先はプラットフォームごとに`dist/ts_smartcut/`（実行体名はOSに準じる）。

方針（ユーザー判断、2026-08-21）:
- ffmpeg/ffprobe/mpv(mpv-2.dll/libmpv-2.dll)は同梱しない。
  docs/TS_SmartCut_Windows_Setup.md記載の通り、実行環境に別途インストールし
  PATHに通しておく必要がある（ADD_DEPENDENCIES.md等は今後必要なら追加）。
- --onedir形式（フォルダ配布）。--onefileはQt6アプリで起動が遅く・不安定に
  なりやすいため避けた。
"""

import os
import sys
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH)

a = Analysis(
    [str(PROJECT_ROOT / "ts_smartcut" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # i18n翻訳JSON。language_manager.pyのfrozen分岐と対応させ、
        # PyInstallerバンドル内で`ts_smartcut/i18n/languages/`という
        # 相対構造を維持したまま配置する。
        (
            str(PROJECT_ROOT / "ts_smartcut" / "i18n" / "languages"),
            "ts_smartcut/i18n/languages",
        ),
        # アプリアイコン(main.pyのsetWindowIcon()用)。
        (
            str(PROJECT_ROOT / "ts_smartcut" / "gui" / "resources"),
            "ts_smartcut/gui/resources",
        ),
    ],
    hiddenimports=[
        # QOpenGLWidgetを使うpreview_widget.py向け。PyInstallerの静的解析
        # だけでは拾いきれないことがあるため明示。
        # Python の QtOpenGL・QtSvg は使わない (QtOpenGLWidgets を読み込んでも QtOpenGL は読み込まれない。2026-11-19)
        "PySide6.QtOpenGLWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # アプリは numpy・LightGBM・scipy を使わない (2026-10-30、設計書 §31.9 の ④⑤)。作る PC の仮想環境に
    # テスト用の numpy が入っていても同梱しない
    # アプリは通信しないので OpenSSL を使う _ssl・_hashlib も入れない (2026-11-15)。hashlib は Python 本体の
    # sha1・sha256 を使う (同じ値)。これで Python の libcrypto-3.dll・libssl-3.dll も入らない
    excludes=["numpy", "lightgbm", "scipy", "ssl", "_ssl", "_hashlib"],
    noarchive=False,
    cipher=block_cipher,
)

# 使っていない Qt の部品を外す（2026-10-28、約 239MB → 下の確かめを参照）。アプリが使うのは QtCore・QtGui・
# QtWidgets・QtOpenGL・QtOpenGLWidgets・QtSvg だけで、PySide6 のフックがそれ以外も拾っていた:
# QML・Quick・仮想キーボード（入力の部品が引き込む）、PDF（画像の読み込みの部品が引き込む）、ネットワーク・TLS
# （アプリは通信しない）、Qt の翻訳（QTranslator を使わない。画面の文字は i18n/languages）。
_UNUSED_QT = (
    "pyside6/qt6qml", "pyside6/qt6quick", "pyside6/qt6virtualkeyboard", "pyside6/plugins/platforminputcontexts/",
    "pyside6/qt6pdf", "pyside6/plugins/imageformats/qpdf",
    "pyside6/qt6network", "pyside6/qtnetwork.", "pyside6/plugins/tls/", "pyside6/plugins/networkinformation/",
    "pyside6/translations/",
    # 使わない Python の Qt の部品 (2026-11-19、約 9 MB)。Qt6OpenGL は QtOpenGLWidgets が使うので残す
    "pyside6/qtopengl.", "pyside6/qtsvg.",
    # ソフトウェアの OpenGL (Windows、19.7 MB)。GPU の OpenGL が無い時のプレビューの予備だったが、リモートデスクトップでも
    # GPU の OpenGL が使えていたので外す (2026-11-19、利用者の判断)。GPU の OpenGL が無い PC ではプレビューが映らないことがある
    "pyside6/opengl32sw.dll",
    # 使わない画面のプラグイン (Windows は qwindows だけ) と画像の形式 (PNG は Qt 本体、ICO だけ残す)。SVG はアイコンの
    # テーマが使う Linux では残す (2026-11-19)
    "pyside6/plugins/platforms/qdirect2d", "pyside6/plugins/platforms/qminimal", "pyside6/plugins/platforms/qoffscreen",
    "pyside6/plugins/generic/qtuiotouchplugin", "pyside6/plugins/iconengines/qsvgicon", "pyside6/plugins/imageformats/qsvg",
    "pyside6/qt6svg.",
    "pyside6/plugins/imageformats/qgif", "pyside6/plugins/imageformats/qicns", "pyside6/plugins/imageformats/qjpeg",
    "pyside6/plugins/imageformats/qtga", "pyside6/plugins/imageformats/qtiff", "pyside6/plugins/imageformats/qwbmp",
    "pyside6/plugins/imageformats/qwebp",
    "pyside6/qt/plugins/platforms/libqminimal", "pyside6/qt/plugins/platforms/libqoffscreen",
    "pyside6/qt/plugins/platforms/libqeglfs", "pyside6/qt/plugins/platforms/libqlinuxfb",
    "pyside6/qt/plugins/platforms/libqvkkhrdisplay", "pyside6/qt/plugins/egldeviceintegrations/",
    "pyside6/qt/plugins/imageformats/libqgif", "pyside6/qt/plugins/imageformats/libqicns",
    "pyside6/qt/plugins/imageformats/libqjpeg", "pyside6/qt/plugins/imageformats/libqtga",
    "pyside6/qt/plugins/imageformats/libqtiff", "pyside6/qt/plugins/imageformats/libqwbmp",
    "pyside6/qt/plugins/imageformats/libqwebp",
    # Qt の TLS の部品が求める OpenSSL。PATH の上の別のソフトのもの (Git など) を拾っていた。その部品は上で
    # 外したので、何も読み込まない (2026-11-15)
    "libcrypto-3-x64.dll", "libssl-3-x64.dll",
    # Linux では同じものが PySide6/Qt/ の下にある (上の書き方では外れず、約 30 MB 残っていた。2026-11-15)
    "pyside6/qt/lib/libqt6qml", "pyside6/qt/lib/libqt6quick", "pyside6/qt/lib/libqt6virtualkeyboard",
    "pyside6/qt/lib/libqt6pdf", "pyside6/qt/lib/libqt6network",
    # 入力の部品は仮想キーボードだけ外す (ibus・compose は日本語の入力・合成の文字に要る)
    "pyside6/qt/plugins/platforminputcontexts/libqtvirtualkeyboard",
    "pyside6/qt/plugins/imageformats/libqpdf", "pyside6/qt/plugins/tls/", "pyside6/qt/plugins/networkinformation/",
    "pyside6/qt/translations/",
    # Qt Network を求める部品 (VNC の画面・TUIO のタッチ。明示して使う時だけ読み込まれる)
    "pyside6/qt/plugins/platforms/libqvnc", "pyside6/qt/plugins/generic/libqtuiotouchplugin",
    # GTK3 の見た目の部品は同梱しない。作る PC の GTK3・ICU (約 42 MB) を引き込むため。GTK3 のある PC にだけ
    # install.sh が入れる (build_release.py が配布物の optional/ に置く。2026-11-15)
    "pyside6/qt/plugins/platformthemes/libqgtk3",
)
# Linux では Qt のライブラリへのリンクが _internal の直下に入ることがある。その名前でも外す
_UNUSED_QT_LIBS = ("libqt6qml", "libqt6quick", "libqt6virtualkeyboard", "libqt6pdf", "libqt6network")


def _used(entry) -> bool:
    name = entry[0].replace("\\", "/").lower()
    return not (name.startswith(_UNUSED_QT) or ("/" not in name and name.startswith(_UNUSED_QT_LIBS)))


a.binaries = [e for e in a.binaries if _used(e)]
a.datas = [e for e in a.datas if _used(e)]

# libmpv は同梱しない (利用者の PC のものを使う。上の方針)。作った PC の libmpv が入ると、その部品
# (ffmpeg・x264 など GPL のものを含む) まで入り、ほかのディストリビューションでは動かないおそれもある
_LIBMPV = [e[0] for e in a.binaries if e[0].replace("\\", "/").rsplit("/", 1)[-1].lower().startswith(("libmpv", "mpv-"))]
if _LIBMPV:
    raise SystemExit(f"libmpv must not be bundled: {_LIBMPV}")


# いつも PC のものを使うライブラリ。PC の libmpv (ffmpeg を通して gio・pango・openal など) と GTK3 の見た目の部品は
# これらを一緒に読み込むので、作った PC (Ubuntu 24.04) の古い版を同梱すると、PC の新しい部品が動かない
# (Arch で: 同梱の libstdc++ で PC の libopenal が読めず libmpv が使えない、同梱の fontconfig で PC の pango が読めず
# GTK3 の見た目が使えない。2026-11-15)。どれも名前 (soname) がディストリビューションをまたいで同じで、glibc 2.39 以降の
# デスクトップには同じか新しい版がある
_SYSTEM_LIBRARIES = (
    "libglib-2.0.so", "libgobject-2.0.so", "libgio-2.0.so", "libgmodule-2.0.so", "libgthread-2.0.so",
    "libstdc++.so.", "libgcc_s.so.", "libfontconfig.so.", "libfreetype.so.", "libpcre2-8.so.", "libffi.so.",
    "libz.so.", "libexpat.so.", "libpng16.so.",
)


def _prune_unused_system_libraries(binaries):
    """Linux: _internal の直下のライブラリのうち、残った部品のどれからも使われないものを外す。

    部品を外すのは解析の後なので、外した部品だけが使っていたライブラリ (Network・TLS の OpenSSL・Kerberos、
    GTK3 の部品の GTK3・ICU など) が残っていた (2026-11-15)。直下に置かれた BINARY を、それ以外 (Python の拡張・
    Qt のライブラリと部品・Python 本体) から ldd の依存をたどって、たどり着けないものを外す。"""
    from PyInstaller.depend import bindepend

    def top_level(entry):
        return "/" not in entry[0].replace("\\", "/") and entry[2] == "BINARY"

    libraries = {e[0]: e for e in binaries if top_level(e) and not e[0].startswith("libpython")}
    queue = [e[1] for e in binaries if e[0] not in libraries]
    seen, reached = set(), set()
    while queue:
        source = queue.pop()
        if source in seen:
            continue
        seen.add(source)
        for name, _path in bindepend.get_imports(source):
            entry = libraries.get(os.path.basename(name))
            if entry is not None and entry[0] not in reached:
                reached.add(entry[0])
                queue.append(entry[1])
    system = {name for name in libraries if name.startswith(_SYSTEM_LIBRARIES)}
    dropped = sorted((set(libraries) - reached) | system)
    if dropped:
        print("TS_SmartCut: leaving out libraries nothing uses or the PC provides: " + ", ".join(dropped))
    return [e for e in binaries if e[0] not in dropped]


if sys.platform.startswith("linux"):
    a.binaries = _prune_unused_system_libraries(a.binaries)

# Linux では同梱するライブラリの記号を取る (libpython などに記号が残り大きかった。2026-11-19)。Windows の DLL は記号を
# 持たないので取らない (strip の道具も無い)
_STRIP = sys.platform.startswith("linux")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TS_SmartCut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=_STRIP,
    upx=False,
    console=False,
    # .exeのアイコン。icon.pngからtools/make_icon.pyで作る(Pillow不要)
    icon=str(PROJECT_ROOT / "ts_smartcut" / "gui" / "resources" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=_STRIP,
    upx=False,
    upx_exclude=[],
    name="TS_SmartCut",
)
