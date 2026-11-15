"""学習 (A8) を、Rust の学習器 (2026-10-30 から) と LightGBM (それまで) で比べる (段階 ④ の確かめ)。

記録 DB は読むだけ (mode=ro で開き、出力フォルダへ写してから使う)。同じ録画・同じ分け方 (新しい 25% を確認用) で
model.train_and_evaluate を 2 回動かし、確認用の録画での再現率・本編を誤って切った時間・良さ・閾値・時間を並べる。
LightGBM は依存から外したので、比べる PC に `pip install lightgbm` で入れる (無ければ Rust の学習器だけ)。
記録 DB には書かない (学習の結果も保存しない)。

使い方:
    python tools/compare_learning.py <出力フォルダ> [--db <cut_history.db>]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut import model as learning  # noqa: E402
from ts_smartcut.autocut.history_db import CutHistoryDB, get_default_history_db_path  # noqa: E402
from ts_smartcut.autocut.tuning import objective  # noqa: E402


def copy_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


class _LightGBMModel:
    """LightGBM の Booster を GbdtModel と同じ呼び方にする (このツールだけ)。"""

    def __init__(self, booster):
        self.booster = booster

    def predict(self, x: bytes, n: int, m: int) -> bytes:
        return np.asarray(self.booster.predict(np.frombuffer(x, "<f8").reshape(n, m)), "<f8").tobytes()


def _lightgbm_fit(x, y, weight):
    import lightgbm as lgb

    x, y, weight = np.asarray(x), np.asarray(y, np.float64), np.asarray(weight, np.float64)

    params = {"objective": "binary", "learning_rate": learning._PARAMS["learning_rate"],
              "num_leaves": learning._PARAMS["num_leaves"], "min_data_in_leaf": learning._PARAMS["min_data_in_leaf"],
              "feature_fraction": learning._PARAMS["feature_fraction"],
              "bagging_fraction": learning._PARAMS["bagging_fraction"],
              "bagging_freq": learning._PARAMS["bagging_freq"], "lambda_l2": learning._PARAMS["lambda_l2"],
              "seed": learning.SEED, "deterministic": True, "num_threads": 0, "verbose": -1}
    data = lgb.Dataset(x, y.astype(np.float64), weight=weight, feature_name=learning.FEATURE_NAMES,
                       categorical_feature=learning.CATEGORICAL, free_raw_data=False)
    return _LightGBMModel(lgb.train(params, data, learning._PARAMS["rounds"]))


def _row(name: str, result, seconds: float) -> str:
    s = result.learned
    return (f"| {name} | {s.recall * 100:.1f}% | {s.precision * 100:.1f}% | {s.wrong_cut:.0f} 秒 | "
            f"{objective(s):.1f} | {result.model.cut_threshold:.2f} | "
            f"{'使う' if result.adoption_problem() is None else '使わない'} | {seconds:.1f} 秒 |")


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
    items = learning.load_items(CutHistoryDB(copy))

    started = time.monotonic()
    ours = learning.train_and_evaluate(items)
    ours_seconds = time.monotonic() - started
    lines = ["# 学習の比べ (Rust の学習器 / LightGBM)", "",
             f"録画 {len(items)} 本 (学習 {ours.train_recordings} 本・確認用 {ours.eval_recordings} 本)、"
             f"区間 {ours.samples} 個", "",
             "| | 再現率 | 適合率 | 本編を誤って切った時間 | 良さ | 切る閾値 | ルールと比べて | 時間 |",
             "|---|---|---|---|---|---|---|---|",
             f"| ルール | {ours.rule.recall * 100:.1f}% | {ours.rule.precision * 100:.1f}% | "
             f"{ours.rule.wrong_cut:.0f} 秒 | {objective(ours.rule):.1f} | — | — | — |",
             _row("Rust の学習器", ours, ours_seconds)]
    try:
        import lightgbm  # noqa: F401
    except Exception:  # noqa: BLE001
        lines.append("| LightGBM | (入っていない) | | | | | | |")
    else:
        original = learning._fit
        learning._fit = _lightgbm_fit
        try:
            started = time.monotonic()
            theirs = learning.train_and_evaluate(items)
            lines.append(_row("LightGBM", theirs, time.monotonic() - started))
        finally:
            learning._fit = original
    text = "\n".join(lines) + "\n"
    print(text)
    (args.out / "compare_learning.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
