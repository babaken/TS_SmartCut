"""記録 DB の局のロゴ画像(.lgd)の材料を確かめる(設計書 §31.9「局のロゴの書き出し」)。

局ごと・隅ごとに、録画の材料を足して不透明度とロゴの色を求め、PNG に描く(左から Y の不透明度、
Y のロゴの色、灰色の背景に重ねた見え方)。.lgd の書き出しは A9 の後で、これは集めた材料が
使えるかを見るためのもの。

使い方:
    python tools/check_logo_images.py <出力フォルダ> [--db <cut_history.db>]
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut.history_db import CutHistoryDB, get_default_history_db_path  # noqa: E402
from ts_smartcut.autocut.logo_image import LogoSamples, solve  # noqa: E402


def write_png(path: Path, gray: np.ndarray) -> None:
    """8 ビットのグレーの PNG を書く。"""
    h, w = gray.shape
    raw = b"".join(b"\x00" + gray[y].astype(np.uint8).tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path)
    parser.add_argument("--db", type=Path, default=get_default_history_db_path())
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.db.is_file():
        sys.exit(f"記録 DB がありません: {args.db}")
    db = CutHistoryDB(args.db)
    args.out.mkdir(parents=True, exist_ok=True)

    with db.get_connection() as conn:
        stations = conn.execute(
            """
            SELECT DISTINCT r.original_network_id, r.service_id,
                   (SELECT r2.service_name FROM recordings r2 WHERE r2.service_id = r.service_id
                    AND r2.original_network_id IS r.original_network_id ORDER BY r2.id DESC LIMIT 1) AS name
            FROM recordings r JOIN logo_samples s ON s.recording_id = r.id ORDER BY r.service_id;
            """
        ).fetchall()
    if not stations:
        print("ロゴ画像の材料がある録画はまだありません")
        return
    for st in stations:
        onid, sid, name = st["original_network_id"], st["service_id"], st["name"] or "?"
        groups = defaultdict(list)  # (フレームの大きさ, 隅) → 材料
        for row in db.get_station_logo_samples(onid, sid):
            samples = LogoSamples.from_blob(row["data"])
            for c in samples.corners:
                groups[(tuple(samples.frame_size), c.corner)].append(c)
        for (size, corner), items in sorted(groups.items()):
            solved = solve(items)
            a, c = np.asarray(solved.alpha[0]), np.asarray(solved.color[0])
            x, y, w, h = solved.box
            seen = a * c + (1 - a) * 128.0
            image = np.concatenate([a * 255.0, c, seen], axis=1)
            out = args.out / f"{name}_{sid}_{size[0]}x{size[1]}_c{corner}.png"
            write_png(out, np.clip(np.round(image), 0, 255))
            inside = a[a > 0.1]
            print(f"{name} (sid {sid}) {size[0]}x{size[1]} 隅 {corner}: 録画 {len(items)} 本、"
                  f"平らな背景のフレーム {sum(i.n for i in items)}、範囲 x={x} y={y} {w}×{h}、"
                  f"不透明度 0.1 超の画素 {len(inside)}(平均 {inside.mean() if len(inside) else 0:.2f}) → {out.name}")


if __name__ == "__main__":
    main()
