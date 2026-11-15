"""記録 DB(cut_history.db)の局のロゴの型と、録画ごとのロゴの一致度を表示する(自動カット A4)。

使い方:
    python tools/check_logos.py [--db <cut_history.db>] [--images <フォルダ>]

- 局ごと: 局名、original_network_id / service_id、型に使った録画の数と期間、ロゴのある隅と
  マスクの画素数。
- 録画ごと: 番組名、ロゴのある隅、残した区間とそれ以外での一致度の平均(本編と CM・番組の前後で
  はっきり分かれるかの確認。計画書 A4 の完了の条件)。
- --images: 局の型の画像(隅ごとの、同じ向きの縁が出ていた割合。白いほどロゴの縁)を PNG で保存する。

DB は読むだけ。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut.features import CORNER_NAMES, RecordingFeatures  # noqa: E402
from ts_smartcut.autocut.history_db import CutHistoryDB, get_default_history_db_path  # noqa: E402
from ts_smartcut.autocut.logo import LogoTemplate, template_from_blob  # noqa: E402


def _corners(has_logo) -> str:
    names = [name for name, yes in zip(CORNER_NAMES, has_logo) if yes]
    return ", ".join(names) if names else "なし"


def _mask_pixels(t: LogoTemplate) -> list:
    return [t.mask_x[i].count_nonzero() + t.mask_y[i].count_nonzero() for i in range(4)]


def _save_image(t: LogoTemplate, path: Path) -> None:
    from PySide6.QtGui import QImage

    tiles = []
    for i in range(4):
        tile = np.zeros((t.gy.shape[1] + 1, t.gx.shape[2] + 1), np.float32)
        tile[:-1, :-1] = np.maximum(np.abs(np.asarray(t.gx)[i][:-1, :]), np.abs(np.asarray(t.gy)[i][:, :-1]))
        tiles.append(np.pad((tile * 255).astype(np.uint8), 1, constant_values=128))
    img = np.ascontiguousarray(np.kron(np.concatenate(tiles, axis=1), np.ones((6, 6), np.uint8)))
    QImage(img.data, img.shape[1], img.shape[0], img.shape[1], QImage.Format.Format_Grayscale8).save(str(path))


def _kept_ranges(conn: sqlite3.Connection, recording_id: int) -> list:
    return conn.execute(
        """
        SELECT c.start_frame, c.end_frame, c.is_excluded FROM cut_ranges c
        JOIN cut_sessions s ON s.id = c.session_id
        WHERE s.recording_id = ? AND s.is_active = 1 ORDER BY c.start_frame;
        """,
        (recording_id,),
    ).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=get_default_history_db_path())
    parser.add_argument("--images", type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.db.is_file():
        sys.exit(f"記録 DB がありません: {args.db}")
    db = CutHistoryDB(args.db)

    print("== 局のロゴの型 ==")
    logos = db.list_station_logos()
    if not logos:
        print("(まだ無い。局ごとに録画が2本以上たまると作る)")
    for row in logos:
        station = f"{row['service_name'] or '?'} (onid=0x{row['original_network_id'] or 0:04X}, sid={row['service_id']})"
        if not row["is_current"]:
            station += " [古い型]"
        if row["logo_data"] is None:
            # ロゴが変わって作り直しを始め、新しい録画がまだ足りない局
            print(f"{station}: 作り直し中({row['generation_start']} 以降の録画が2本たまると作る)")
            continue
        t = template_from_blob(row["logo_data"])
        print(
            f"{station}: "
            f"録画 {row['recordings_count']} 本 {row['valid_from']} 〜 {row['valid_to']}、"
            f"ロゴのある隅: {_corners(t.has_logo)}、マスクの画素 {_mask_pixels(t)}"
        )
        if args.images:
            args.images.mkdir(parents=True, exist_ok=True)
            path = args.images / f"logo_{row['original_network_id']}_{row['service_id']}.png"
            _save_image(t, path)
            print(f"  画像: {path}")

    print("\n== 録画ごとのロゴの一致度 ==")
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.service_name, r.program_name, f.data FROM recordings r
            JOIN features f ON f.recording_id = r.id WHERE f.feature_version >= 2 AND f.is_complete = 1 ORDER BY r.id;
            """
        ).fetchall()
        for row in rows:
            f = RecordingFeatures.from_blob(row["data"])
            t = f.logo_template
            line = f"[{row['id']}] {row['service_name'] or '?'} {row['program_name'] or ''}: "
            if t is None or not any(t.has_logo):
                print(line + "ロゴなし")
                continue
            labels = np.zeros(f.frame_count, np.int8)
            for start, end, excluded in _kept_ranges(conn, row["id"]):
                labels[start:end] = -1 if excluded else 1
            parts = []
            for i in [k for k, yes in enumerate(t.has_logo) if yes]:
                s = np.asarray(f.logo_score[i], np.float32) / 255
                kept, other = s[labels == 1], s[labels == 0]
                parts.append(
                    f"{CORNER_NAMES[i]} 残した区間 {kept.mean() if len(kept) else float('nan'):.2f} / "
                    f"それ以外 {other.mean() if len(other) else float('nan'):.2f}"
                )
            print(line + "; ".join(parts))


if __name__ == "__main__":
    main()
