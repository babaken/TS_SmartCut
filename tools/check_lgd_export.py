"""局ロゴ (.lgd) の書き出しと、取り込みの型の作り方の変更を、本物の記録 DB で確かめる (2026-10-30、③-b)。

記録 DB は読むだけ (mode=ro で開き、出力フォルダへ写してから使う)。次の 2 つを行う:

1. 取り込んだ .lgd の型: DB に残っている元の .lgd から今の計算 (Rust、背景のノイズの乱数が numpy と違う) で型を
   作り直し、DB の型 (取り込んだ時の numpy の計算) と比べる。ロゴのある隅が同じで、縁の向きの合い方
   (logo.two_way_agreement) が高ければ、このアプリのカットで今までどおり使える。
2. 書き出し: 材料のある局の .lgd を <出力フォルダ>/lgd/ に書き出し (アプリのボタンと同じ lgd_export)、同じ局の
   取り込んだ .lgd (エンコードソフトが作ったもの) と位置・大きさ・ロゴの画素の不透明度と輝度を比べる。書き出した
   .lgd はエンコードソフトのロゴのフォルダに置いて、読み込まれるか・ロゴ消しとロゴの検出に使えるかを確かめる。

結果は画面と <出力フォルダ>/check_lgd_export.md (局はサービス ID だけで書く。局名は出さない)。

使い方:
    python tools/check_lgd_export.py <出力フォルダ> [--db <cut_history.db>]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut.history_db import CutHistoryDB, get_default_history_db_path  # noqa: E402
from ts_smartcut.autocut.lgd_export import export_station_lgd, station_logo_report  # noqa: E402
from ts_smartcut.autocut.lgd_import import parse_lgd, read_lgd, template_from_lgd  # noqa: E402
from ts_smartcut.autocut.logo import template_from_blob, two_way_agreement  # noqa: E402


def copy_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def check_imported(db: CutHistoryDB) -> list:
    lines = ["## 1. 取り込んだ .lgd の型 (DB の型 = numpy、今の計算 = Rust)", "",
             "| サービス ID | ファイル | ロゴのある隅 (DB / 今) | 合い方 |", "|---|---|---|---|"]
    with db.get_connection() as conn:
        rows = conn.execute("SELECT service_id, file_name, logo_data, lgd_data FROM imported_logos "
                            "ORDER BY service_id, file_name;").fetchall()
    for row in rows:
        stored = template_from_blob(row["logo_data"])
        logos = parse_lgd(bytes(row["lgd_data"]), row["file_name"])
        new = template_from_lgd(logos[0]) if len(logos) == 1 else None
        corners = "".join("○" if v else "・" for v in stored.has_logo)
        if new is None:
            lines.append(f"| {row['service_id']} | {row['file_name']} | {corners} / なし | — |")
            continue
        agreement = two_way_agreement(stored, new)
        lines.append(f"| {row['service_id']} | {row['file_name']} | {corners} / "
                     f"{''.join('○' if v else '・' for v in new.has_logo)} | "
                     f"{'—' if agreement is None else f'{agreement:.3f}'} |")
    return lines


def _stats(lgd) -> str:
    alpha, luma = np.asarray(lgd.alpha), np.asarray(lgd.luma)
    inside = alpha > 0.1
    if not inside.any():
        return "ロゴの画素なし"
    return (f"x={lgd.x} y={lgd.y} {lgd.width}×{lgd.height}、不透明度 0.1 超 {int(inside.sum())} 画素 "
            f"(平均 {alpha[inside].mean():.2f}、輝度 {luma[inside].mean():.0f})")


def check_export(db: CutHistoryDB, out: Path) -> list:
    lines = ["## 2. 書き出し (今のロゴの世代、局の型でロゴのある隅、材料のある録画 2 本以上)", "",
             "| サービス ID | 書き出したファイル | 映像 | 書き出したロゴ | 取り込んだ .lgd |", "|---|---|---|---|---|"]
    skipped_lines = ["", "### 書き出さなかった隅", "", "| サービス ID | 映像 | 隅 | 録画 | 理由 |", "|---|---|---|---|---|"]
    with db.get_connection() as conn:
        stations = conn.execute(
            "SELECT DISTINCT r.original_network_id, r.service_id FROM recordings r "
            "JOIN logo_samples s ON s.recording_id = r.id ORDER BY r.service_id;").fetchall()
    for st in stations:
        onid, sid = st["original_network_id"], st["service_id"]
        _images, skipped = station_logo_report(db, onid, sid)
        for s in skipped:
            skipped_lines.append(f"| {sid} | {s.frame_size[0]}×{s.frame_size[1]} | {s.corner} | {s.recordings} | "
                                 f"{s.reason} |")
        written = export_station_lgd(db, onid, sid, f"SID{sid}", out)
        imported = db.get_imported_logo(sid)
        reference = parse_lgd(bytes(imported["lgd_data"]))[0] if imported is not None else None
        if not written:
            lines.append(f"| {sid} | (書き出せない) | | | {_stats(reference) if reference else '—'} |")
            continue
        for path in written:
            lgd = read_lgd(path)[0]
            same_size = reference is not None and (reference.image_width, reference.image_height) == (
                lgd.image_width, lgd.image_height)
            lines.append(f"| {sid} | {path.name} | {lgd.image_width}×{lgd.image_height} | {_stats(lgd)} | "
                         f"{_stats(reference) if same_size else '— (同じ映像の大きさのものが無い)'} |")
    return lines + skipped_lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path)
    parser.add_argument("--db", type=Path, default=get_default_history_db_path())
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.db.is_file():
        sys.exit(f"記録 DB がありません: {args.db}")
    args.out.mkdir(parents=True, exist_ok=True)
    copy = args.out / "cut_history_copy.db"
    if copy.exists():
        sys.exit(f"写しが既にあります (消してからやり直す): {copy}")
    copy_db(args.db, copy)
    db = CutHistoryDB(copy)
    lines = ["# .lgd の書き出しと取り込みの型の確かめ", ""] + check_imported(db) + [""]
    lines += check_export(db, args.out / "lgd")
    text = "\n".join(lines) + "\n"
    print(text)
    (args.out / "check_lgd_export.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
