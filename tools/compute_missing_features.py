"""記録 DB で特徴(音量・場面・ロゴ)が無い・古い録画の特徴を、後から計算する(自動カット A3/A4)。

特徴はバッチ処理が全部終わった後に計算するので、その前に元の録画を移動すると計算できない
(2026-10-08、録画の移動先のフォルダ)。移動先のフォルダを指定すると、同じ名前の
ファイルを探し、サイズと先頭 16MB のハッシュが記録と同じなら、記録した残す区間で計算して保存し、
局のロゴの型も作り直す。アプリが起動中でも使える(DB は WAL)。

--recording で番号(autocut_eval.py の [番号])を指定すると、計算済みでもその録画だけを計算し直す
(音量が空になった録画、2026-10-11)。

--logo-samples を付けると、局のロゴ画像(.lgd)の材料が無い録画も計算し直す(材料は特徴と同じ
1回の読み込みで集めるので、特徴も計算し直す。2026-10-13 より前にカットした録画には材料が無い)。

使い方:
    python tools/compute_missing_features.py <フォルダ> [<フォルダ> ...] [--db <cut_history.db>] [--dry-run]
        [--recording <番号> ...] [--logo-samples]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut.feature_worker import FeatureJob, FeatureWorker  # noqa: E402
from ts_smartcut.autocut.features import FEATURE_VERSION, KeepRange  # noqa: E402
from ts_smartcut.autocut.history_db import CutHistoryDB, get_default_history_db_path  # noqa: E402
from ts_smartcut.autocut.recorder import compute_file_hash  # noqa: E402


def missing(db: CutHistoryDB, recording_ids: Optional[List[int]] = None, logo_samples: bool = False) -> list:
    """特徴が無い・版が古い・途中までの録画(logo_samples なら、ロゴ画像の材料が無い録画も)。
    recording_ids を渡すと、その録画(計算済みでも)。"""
    with db.get_connection() as conn:
        if recording_ids:
            marks = ",".join("?" * len(recording_ids))
            return [dict(r) for r in conn.execute(
                f"""
                SELECT id, file_name, file_size, file_hash, fps, service_id
                FROM recordings WHERE id IN ({marks}) ORDER BY id;
                """,
                recording_ids,
            )]
        return [dict(r) for r in conn.execute(
            """
            SELECT r.id, r.file_name, r.file_size, r.file_hash, r.fps, r.service_id
            FROM recordings r LEFT JOIN features f ON f.recording_id = r.id
                              LEFT JOIN logo_samples s ON s.recording_id = r.id
            WHERE f.recording_id IS NULL OR f.feature_version < ? OR f.is_complete = 0
               OR (? AND s.recording_id IS NULL)
            ORDER BY r.id;
            """,
            (FEATURE_VERSION, 1 if logo_samples else 0),
        )]


def keep_ranges(db: CutHistoryDB, recording_id: int) -> list:
    # 話ごとに分けて変換した録画は、すべての話の区間(有効なカットすべて)
    return [KeepRange(r["start_frame"], r["end_frame"], bool(r["is_excluded"]))
            for r in db.get_active_cut_ranges(recording_id)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--db", type=Path, default=get_default_history_db_path())
    parser.add_argument("--dry-run", action="store_true", help="見つかった録画を表示するだけ")
    parser.add_argument("--recording", type=int, nargs="+", help="計算済みでも計算し直す録画の番号")
    parser.add_argument("--logo-samples", action="store_true", help="ロゴ画像の材料が無い録画も計算し直す")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    if not args.db.is_file():
        sys.exit(f"記録 DB がありません: {args.db}")
    db = CutHistoryDB(args.db)

    jobs = []
    for rec in missing(db, args.recording, args.logo_samples):
        found = None
        for folder in args.folders:
            path = folder / rec["file_name"]
            if path.is_file() and path.stat().st_size == rec["file_size"] and compute_file_hash(path) == rec["file_hash"]:
                found = path
                break
        print(f"[{rec['id']}] {'見つかった: ' + str(found) if found else '見つからない'}  {rec['file_name']}")
        if found is not None and rec["fps"]:
            jobs.append(FeatureJob(rec["id"], found, rec["fps"], keep_ranges(db, rec["id"]), rec["service_id"]))
    if args.dry_run or not jobs:
        return

    print(f"{len(jobs)} 本の特徴を計算します")
    worker = FeatureWorker(db=db)
    worker.submit(jobs)
    try:
        worker.wait_idle()
    except KeyboardInterrupt:
        print("止めます(計算中の録画は、そこまでの分を保存します)")
        worker.stop(timeout=60)


if __name__ == "__main__":
    main()
