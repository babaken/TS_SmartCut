"""しきい値の自動補正の速さを測る (速さの改善の基準は遅い PC)。

記録 DB の写しで tuning.tune_with_holdout(バッチの後の自動補正と同じ計算。DB には書かない)を動かし、
並べるスレッドの数(workers)ごとの時間を測る。記録の読み込みにかかる時間も別に測る。評価は Rust だけ
(ts_smartcut_native が要る。2026-10-28 に Python の評価と、別のプロセスで並べる方式は削った)。

本物の記録 DB は読むだけ(mode=ro で開き、出力フォルダへ写してから使う)。結果は <出力フォルダ>/tuning_speed.json と、
PR のコメントに貼れる tuning_speed.md。局は「全局共通・録画の最も多い局・真ん中の局」とだけ書く(局の鍵は出さない)。

使い方:
    python tools/measure_tuning_speed.py --db <cut_history.db> [--out <出力フォルダ>]
        [--workers 1,2,4,8] [--scopes all,largest,median]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import time
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut import tuning  # noqa: E402
from ts_smartcut.autocut.history_db import CutHistoryDB  # noqa: E402
from ts_smartcut.autocut.native import native_module  # noqa: E402
from ts_smartcut.autocut.thresholds import station_key  # noqa: E402

def copy_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def scopes_of(db: CutHistoryDB, names: List[str]) -> List[tuple]:
    counts = sorted(((s["recordings"], station_key(s["original_network_id"], s["service_id"]))
                     for s in db.list_stations() if s["recordings"] >= tuning.MIN_TUNE_RECORDINGS), reverse=True)
    result = []
    for name in names:
        if name == "all":
            result.append(("全局共通", ""))
        elif name == "largest" and counts:
            result.append(("録画の最も多い局", counts[0][1]))
        elif name == "median" and len(counts) >= 3:
            result.append(("真ん中の局", counts[len(counts) // 2][1]))
    return result


def run(db: CutHistoryDB, scope_key: str, workers: int) -> dict:
    started = time.monotonic()
    result = tuning.tune_with_holdout(db, db.get_threshold_rows(), scope_key, workers=workers)
    return {"seconds": round(time.monotonic() - started, 1), "recordings": result.recordings, "folds": result.folds,
            "changes": sorted(result.changes), "objective": round(tuning.objective(result.before), 3)}


def markdown(report: dict) -> str:
    m = report["machine"]
    lines = [f"## しきい値の自動補正の速さ（{time.strftime('%Y-%m-%d')}）", "",
             f"OS {m['os']}・論理 CPU {m['cpus']}・Python {m['python']}・ts_smartcut_native {m['native']}・"
             f"自動で決まる並べる数 {m['auto_workers']}", "",
             "### 1. 補正の全体（秒）", "",
             "| 範囲 | 録画 | 外す回数 | 記録の読み込み | 並べる数 | 秒 | 変えた項目 |", "|---|---|---|---|---|---|---|"]
    for r in report["runs"]:
        lines.append(f"| {r['scope']} | {r['recordings']} | {r['folds']} | {r['load']} | {r['workers']} | "
                     f"{r['seconds']} | {', '.join(r['changes']) or 'なし'} |")
    same = report.get("same_result")
    if same is not None:
        lines += ["", f"並べる数が違っても補正の結果（変えた項目・補正前の良さ）が同じ: {same}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="記録 DB(読むだけ。写してから使う)")
    parser.add_argument("--out", type=Path, default=Path("speed_results"))
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--scopes", default="all,largest,median")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    copy = args.out / "cut_history_copy.db"
    copy.unlink(missing_ok=True)
    copy_db(args.db, copy)
    db = CutHistoryDB(copy)
    native = native_module()
    report = {"machine": {"os": platform.platform(terse=True), "python": platform.python_version(),
                          "cpus": os.cpu_count(), "native": native.VERSION, "auto_workers": tuning.worker_count(1000)},
              "runs": []}
    results = {}
    for label, key in scopes_of(db, args.scopes.split(",")):
        started = time.monotonic()
        tuning.load_samples(db, key)
        load = round(time.monotonic() - started, 2)
        for w in [int(x) for x in args.workers.split(",") if x]:
            print(f"[{label}] workers={w}", flush=True)
            r = run(db, key, w)
            report["runs"].append({"scope": label, "load": load, "workers": w, **r})
            results.setdefault(label, set()).add((tuple(r["changes"]), r["objective"]))
            (args.out / "tuning_speed.md").write_text(markdown(report), encoding="utf-8")
    if results:
        report["same_result"] = all(len(v) == 1 for v in results.values())
    (args.out / "tuning_speed.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "tuning_speed.md").write_text(markdown(report), encoding="utf-8")
    for path in (copy, Path(f"{copy}-wal"), Path(f"{copy}-shm")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            print(f"写しを消せなかった(後で消す): {path}")
    print(f"結果: {args.out / 'tuning_speed.md'}")


if __name__ == "__main__":
    main()
