"""ts_smartcut/gui/resources/icon.png から Windows 用の icon.ico を作る。

    python tools/make_icon.py

.exe のアイコン(ts_smartcut.spec の icon=)に使う。16〜256px の各サイズを
PNG のまま ICO に入れる(Windows Vista 以降が読める形式)。Pillow は使わず、
縮小は Qt で行う。icon.png を差し替えたらこれを実行し直す。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage

RESOURCES = Path(__file__).resolve().parent.parent / "ts_smartcut" / "gui" / "resources"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _png_bytes(image: QImage, size: int) -> bytes:
    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.save(buffer, "PNG")
    return bytes(data)


def make_ico(source: Path, dest: Path) -> None:
    image = QImage(str(source))
    if image.isNull():
        raise SystemExit(f"読み込めません: {source}")
    images = [(size, _png_bytes(image, size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        # 幅・高さの 0 は 256 を表す
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    dest.write_bytes(header + entries + payload)


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    make_ico(RESOURCES / "icon.png", RESOURCES / "icon.ico")
    print(RESOURCES / "icon.ico")
