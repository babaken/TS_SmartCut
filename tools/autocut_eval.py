"""記録 DB のカットで、自動カットの提案(ルール A5)の一致率を出す(自動カット計画 A7)。

使い方:
    python tools/autocut_eval.py [--db <cut_history.db>] [--detail] [--defaults] [--recording ID ...]

- 録画ごと・局ごと・地上波/BS/CS ごと・全体の表(ルールの値は、その録画自身を除いた
  過去のカットから測る)。
- --detail: 録画ごとに、案と正解の残す区間を並べて出す。
- --defaults: 記録 DB のしきい値(局の値・全局共通の値)を使わず、初期値で評価する(比べるため)。

DB は読むだけ。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut.evaluate import EXACT, MISS, NEAR, SECOND, Score, evaluate_all, summarize  # noqa: E402
from ts_smartcut.autocut.history_db import CutHistoryDB, get_default_history_db_path  # noqa: E402

TYPE_NAMES = {"terrestrial": "地上波", "bs": "BS", "cs": "CS"}


def _row(name: str, s: Score) -> str:
    edges = s.edge_count or 1
    return (
        f"| {name} | {s.recordings} | {s.recall * 100:.1f}% | {s.precision * 100:.1f}% | "
        f"{s.wrong_cut:.1f}秒 | {s.truth_cut:.0f}秒 | "
        f"{s.edges[EXACT] / edges * 100:.0f}% | {(s.edges[EXACT] + s.edges[NEAR]) / edges * 100:.0f}% | "
        f"{(s.edges[EXACT] + s.edges[NEAR] + s.edges[SECOND]) / edges * 100:.0f}% | {s.edges[MISS]} | {s.extra_edges} |"
    )


HEADER = (
    "| | 本数 | 再現率 | 適合率 | 本編を切った | 正解で切った | 境目 ±0 | ±2f 以内 | ±1秒以内 | 外れ | 余分な境目 |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def _frames(ranges, fps) -> str:
    return ", ".join(f"{a}-{b}" + ("(基準外)" if len(r) > 2 and r[2] else "")
                     for r in ranges for a, b in [r[:2]])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=get_default_history_db_path())
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--recording", type=int, nargs="*")
    parser.add_argument("--defaults", action="store_true", help="しきい値を初期値で評価する")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.db.is_file():
        sys.exit(f"記録 DB がありません: {args.db}")
    results = evaluate_all(CutHistoryDB(args.db), args.recording, defaults=args.defaults)
    if not results:
        sys.exit("評価できる録画がありません(特徴と記録したカットのある録画が必要)")

    print("## 録画ごと\n")
    print(HEADER)
    for r in results:
        print(_row(f"[{r.recording_id}] {r.station} {r.program[:20]}", r.score))
    print("\n## 局ごと\n")
    print(HEADER)
    for name, s in sorted(summarize(results, lambda r: r.station).items()):
        print(_row(name, s))
    print("\n## 地上波/BS/CS\n")
    print(HEADER)
    for name, s in summarize(results, lambda r: TYPE_NAMES.get(r.broadcast_type, r.broadcast_type)).items():
        print(_row(name, s))
    total = Score()
    for r in results:
        total.add(r.score)
    print("\n## 全体\n")
    print(HEADER)
    print(_row("全体", total))

    if args.detail:
        print("\n## 案と正解\n")
        for r in results:
            print(f"[{r.recording_id}] {r.station} {r.program}")
            print(f"  正解: {_frames(r.truth, None)}")
            for p in r.proposals:
                print(f"  案:   {p.start}-{p.end} 確信度 {p.confidence:.1f}  {p.reason}")


if __name__ == "__main__":
    main()
