"""しきい値の自動補正 (設計書 §31.9「ルールのしきい値」、自動カット計画「A8 の後の作業」1)。

記録したカットを正解として、しきい値(thresholds.py)を1つずつ試す値(ThresholdSpec.grid)に
動かし、評価(evaluate.compare)が良くなる値を選ぶ(座標ごとの山登り。全体が変わらなく
なるか MAX_PASSES 回まで)。

- 局: 録画が MIN_TUNE_RECORDINGS 本以上の局だけ、その局の録画で計算する(2026-10-14 に 5 本から 2 本へ。
  本数の少ない局は下の「補正に使っていない録画での評価」で採用を決める)。
- 全局共通: 全局の録画で計算する(局の値を持つ項目は、その局では全局共通の値を使わないので
  変わらない)。
- 手で直した値(source=manual)は動かさない(2026-10-15 から手入力は無いが、前の版で書いた行のため)。
- 採用は自動(auto_tuning.py、2026-10-15 利用者の決定): 補正に使っていない録画で今の値より良く、その
  再現率が MAX_RECALL_DROP より下がらない時だけ(adoption_problem)。

良さ = 正しく切った秒 − WRONG_CUT_WEIGHT × 本編を切った秒 + EDGE_WEIGHT × ±2 フレーム以内に
合った境目の数。本編を誤って切るのは取り戻せない誤りなので重くする。

補正に使っていない録画での評価(tune_with_holdout、2026-10-14 決定): 補正に使った録画での評価は、
本数の少ない局ほど実際より良く見えた(2 本の局で、1 本で補正すると、もう 1 本では何も切らなくなった)。
局の補正は録画を 1 本ずつ外して残りで補正し、外した録画で評価した合計(1 本抜き)、全局共通は局を 1 つずつ
外して補正し、外した局で評価した合計を出す。補正を何度も繰り返すので、スレッドで論理 CPU の半分まで並べる
(評価は Rust で、計算の間は GIL を外すので並ぶ。録画の特徴は 1 つを共有する)。

評価 1 回(evaluate)は Rust (native/src/tuning.rs) で行う。境目の候補・区間の判定・案・比較の決まりは、案を
作る時と同じ native/src/rules.rs(2026-10-28 から 1 か所。それまでは Python の candidates.py・rules.py・
proposals.py・evaluate.compare と tuning.rs の 2 つ)。録画の特徴の Rust 側の写しも案づくりと共有する。

評価は案を作る時と同じく、局の値(区間の終わりの差・短い区間を残すか、proposals.StationProfile)を
試す値ごとに、その録画を除いた同じ局の録画から測り直す(固定すると、補正の途中では良く
見えた値が、測り直すと本編を多く切った。GR-C で確認、2026-10-14)。境目の候補は値の組ごとに
覚えておく(Rust の中で)。評価は補正に使ったのと同じ録画で計算するので、新しい録画では少し下がる見込み。
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple


from ts_smartcut.autocut.evaluate import EXACT, MISS, NEAR, SECOND, Score
from ts_smartcut.autocut.features import RecordingFeatures
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.proposals import keep_ranges, program_window
from ts_smartcut.autocut.candidates import recording
from ts_smartcut.autocut.rules import ProgramWindow
from ts_smartcut.autocut.thresholds import (
    AUTO,
    MANUAL,
    SPEC_BY_NAME,
    SPECS,
    Rows,
    ThresholdRow,
    Thresholds,
    consistency_error,
    resolve,
    station_key,
)

logger = logging.getLogger(__name__)

MIN_TUNE_RECORDINGS = 2  # 局ごとに補正する録画の本数(設計書 §31.9。2026-10-14 に 5 本から 2 本へ)
WRONG_CUT_WEIGHT = 10.0
EDGE_WEIGHT = 2.0
MAX_PASSES = 3
# これより小さい良くなり方(良さの単位 = 秒)では値を変えない(録画のたまたまの差で値が揺れないように)
MIN_GAIN = 1.0
# 補正に使っていない録画での再現率がこれ以上下がる補正は採用しない(何も切らなくなる補正を防ぐ。
# 2026-10-14 の確かめで CS-F が 58% → 0%。2026-10-15 利用者の決定で 5 ポイント)
MAX_RECALL_DROP = 0.05



class TuningCancelled(Exception):
    pass


@dataclass
class TuneSample:
    recording_id: int
    station_key: str
    # load_samples は Rust の写しを作ったら None にする(下の native_sample だけが使う)
    features: Optional[RecordingFeatures]
    truth: List[Tuple[int, int, bool]]
    window: Optional[ProgramWindow]
    _native: object = field(default=None, repr=False)

    def native_sample(self, native):
        """Rust の評価(native.evaluate_samples)に渡す同じ録画(作ったら覚えておく)。特徴は案づくりと同じ
        Rust 側の写し(candidates.recording)を共有する。"""
        if self._native is None:
            self._native = native.TuneSample(
                self.recording_id, self.station_key, recording(self.features),
                [(int(a), int(b), bool(ex)) for a, b, ex in self.truth],
                None if self.window is None else (float(self.window.start), float(self.window.end)))
        return self._native


@dataclass
class TuneResult:
    scope_key: str  # '' = 全局共通
    recordings: int
    before: Score
    after: Score
    changes: Dict[str, Tuple[float, float]]  # 項目 → (今の値, 新しい値)。変わった項目だけ
    # 補正に使っていない録画での評価(1 本抜き・局を 1 つずつ抜く)。出せない時(外すと補正に使う録画が
    # 残らない)は None。folds は外した回数
    holdout: Optional[Score] = None
    folds: int = 0

    @property
    def holdout_worse(self) -> bool:
        """補正に使っていない録画で、今の値より悪くなったか(良さ objective で比べる)。"""
        return self.holdout is not None and bool(objective(self.holdout) < objective(self.before))


def adoption_problem(result: TuneResult) -> Optional[str]:
    """自動で採用しない理由(ログ用の英語)。採用してよければ None。"""
    if not result.changes:
        return "no change"
    if result.holdout is None:
        return "no recordings left out to check the values with"
    if not objective(result.holdout) > objective(result.before):
        return (f"not better on recordings left out (score {objective(result.holdout):.1f} "
                f"vs {objective(result.before):.1f} now)")
    if result.holdout.recall < result.before.recall - MAX_RECALL_DROP:
        return (f"recall on recordings left out drops from {result.before.recall * 100:.1f}% "
                f"to {result.holdout.recall * 100:.1f}%")
    return None


def objective(score: Score) -> float:
    return float(score.correct_cut - WRONG_CUT_WEIGHT * score.wrong_cut
                 + EDGE_WEIGHT * (score.edges[EXACT] + score.edges[NEAR]))


def load_samples(db, scope_key: str = "", stop_event=None,
                 progress: Optional[Callable[[int, int], None]] = None) -> List[TuneSample]:
    """補正に使う録画(最後まで計算した特徴と有効なカットのあるもの)。scope_key が局ならその局だけ、'' なら全局。

    録画ごとに Rust の写し(native_sample)を作ったら Python 側の特徴は捨てる: 両方を持つと全録画の特徴が
    2 倍になっていた(2026-11-19、記録の写しの 314 本で最大 512 MB → 約 260 MB)。features は None になる。"""
    native = native_module()
    with db.get_connection() as conn:
        records = [dict(r) for r in conn.execute(
            """
            SELECT r.id, r.original_network_id, r.service_id, r.recording_start, r.broadcast_start,
                   r.broadcast_duration
            FROM recordings r JOIN features f ON f.recording_id = r.id
            WHERE f.feature_version >= 2 AND f.is_complete = 1 AND r.service_id IS NOT NULL ORDER BY r.id;
            """
        )]
    records = [r for r in records
               if not scope_key or station_key(r["original_network_id"], r["service_id"]) == scope_key]
    samples: List[TuneSample] = []
    for k, r in enumerate(records):
        if stop_event is not None and stop_event.is_set():
            raise TuningCancelled()
        if progress is not None:
            progress(k, len(records))
        truth = keep_ranges(db, r["id"])
        if not truth:
            continue
        row = db.get_features_row(r["id"])
        try:
            features = RecordingFeatures.from_blob(row["data"])
        except Exception:  # noqa: BLE001
            logger.warning("Could not read features: recording_id=%s", r["id"], exc_info=True)
            continue
        window = program_window(r["recording_start"], r["broadcast_start"], r["broadcast_duration"])
        sample = TuneSample(r["id"], station_key(r["original_network_id"], r["service_id"]), features, truth, window)
        sample.native_sample(native)
        sample.features = None
        samples.append(sample)
    return samples


def evaluate(samples: List[TuneSample], thresholds_for: Callable[[str], Thresholds],
             score_ids: Optional[set] = None) -> Score:
    """samples の案を作って正解と比べた合計(Rust、native.evaluate_samples)。局の値はその録画を除いた
    同じ局の録画から測る(proposals.recording_proposals と同じ)。score_ids を渡すと、その録画だけを数える
    (局の値はほかの録画からも測る)。"""
    native = native_module()
    table = {}
    for s in samples:
        if s.station_key not in table:
            t = thresholds_for(s.station_key)
            table[s.station_key] = [float(getattr(t, spec.name)) for spec in SPECS]
    values = native.evaluate_samples([s.native_sample(native) for s in samples], table,
                                     None if score_ids is None else set(score_ids))
    truth_cut, proposed_cut, correct_cut, wrong_cut, exact, near, second, miss, extra, recordings = values
    return Score(truth_cut, proposed_cut, correct_cut, wrong_cut,
                 {EXACT: exact, NEAR: near, SECOND: second, MISS: miss}, extra, recordings)


def _with_values(rows: Rows, scope_key: str, values: Dict[str, float]) -> Dict[str, Dict[str, ThresholdRow]]:
    """rows の scope_key の段に values を(自動補正の行として)重ねたもの。"""
    result = {k: dict(v) for k, v in rows.items()}
    level = result.setdefault(scope_key, {})
    for name, value in values.items():
        row = level.get(name)
        if row is None or row.value != value:
            level[name] = ThresholdRow(value, AUTO)
    return result


def tune(samples: List[TuneSample], rows: Rows, scope_key: str = "",
         stop_event=None, progress: Optional[Callable[[int, int], None]] = None,
         log: bool = True) -> TuneResult:
    """scope_key('' = 全局共通)の段の値を補正する。samples は load_samples(db, scope_key)。"""
    level = rows.get(scope_key, {})
    tunable = [s for s in SPECS if level.get(s.name) is None or level[s.name].source != MANUAL]
    start = resolve(rows, scope_key, warn=False)
    current = {s.name: getattr(start, s.name) for s in tunable}
    keys = sorted({s.station_key for s in samples})

    def thresholds_for_values(values: Dict[str, float]) -> Callable[[str], Thresholds]:
        layered = _with_values(rows, scope_key, values)
        table = {key: resolve(layered, key, warn=False) for key in keys}
        return lambda key: table[key]

    def score_of(values: Dict[str, float]) -> Score:
        return evaluate(samples, thresholds_for_values(values))

    before = score_of(current)
    after = before
    best = objective(before)
    total = MAX_PASSES * sum(len(s.grid()) for s in tunable)
    done = 0
    for _pass in range(MAX_PASSES):
        at_start = dict(current)
        for spec in tunable:
            for value in spec.grid():
                done += 1
                if progress is not None:
                    progress(done, total)
                if stop_event is not None and stop_event.is_set():
                    raise TuningCancelled()
                if value == current[spec.name]:
                    continue
                trial = dict(current, **{spec.name: value})
                if consistency_error(start.replace(**trial)) is not None:
                    continue
                score = score_of(trial)
                gain = objective(score) - best
                if gain > MIN_GAIN:
                    best += gain
                    current, after = trial, score
        if current == at_start:
            break
    if progress is not None:
        progress(total, total)
    changes = {name: (getattr(start, name), value) for name, value in current.items()
               if value != getattr(start, name)}
    if log:
        logger.info("Tuned thresholds for %s: %d recordings, changes %s", scope_key or "all stations",
                    len(samples), {n: v for n, (_old, v) in changes.items()})
    return TuneResult(scope_key, len(samples), before, after, changes)


def changes_to_rows(result: TuneResult) -> Dict[tuple, ThresholdRow]:
    """採用した時に書く行((局の鍵, 項目) → 行)。"""
    return {(result.scope_key, name): ThresholdRow(new, AUTO) for name, (_old, new) in result.changes.items()
            if SPEC_BY_NAME[name].accepts(new)}


# ---- 補正に使っていない録画での評価(1 本抜き・局を 1 つずつ抜く) ----------------------------


def holdout_folds(samples: Sequence[TuneSample], scope_key: str) -> List[List[int]]:
    """外す録画の組。局の補正は録画 1 本ずつ、全局共通は局 1 つずつ。外すと補正に使う録画が
    残らない(局の録画が 1 本・全局で局が 1 つ)時は空。"""
    if scope_key:
        ids = [s.recording_id for s in samples]
        return [[i] for i in ids] if len(ids) >= 2 else []
    by_station: Dict[str, List[int]] = {}
    for s in samples:
        by_station.setdefault(s.station_key, []).append(s.recording_id)
    return list(by_station.values()) if len(by_station) >= 2 else []


def run_fold(samples: List[TuneSample], rows: Rows, scope_key: str, held: Sequence[int],
             stop_event=None) -> Score:
    """held の録画を外して補正し、外した録画を補正した値で評価する。"""
    held_set = set(held)
    train = [s for s in samples if s.recording_id not in held_set]
    result = tune(train, rows, scope_key, stop_event=stop_event, log=False)
    layered = _with_values(rows, scope_key, {n: v for n, (_old, v) in result.changes.items()})
    table: Dict[str, Thresholds] = {}

    def thresholds_for(key: str) -> Thresholds:
        if key not in table:
            table[key] = resolve(layered, key, warn=False)
        return table[key]

    return evaluate(samples, thresholds_for, score_ids=held_set)


def worker_count(tasks: int) -> int:
    """並べるスレッドの数(利用者の設定は無く、ここで決める)。使える論理 CPU の半分(ほぼ実際のコアの数)まで:
    それより増やしてもほとんど速くならず(ノート PC 8 コアで 4→8 本が 1.23 倍、デスクトップ 論理 20 で
    8→20 本が 1.08 倍。2026-10-27)、補正はバッチの後に裏で動くので、プレビューなどの画面の分を残す。仕事の数まで。"""
    cpus = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    return max(1, min(cpus // 2, tasks))


def tune_with_holdout(db, rows: Rows, scope_key: str = "", stop_event=None,
                      progress: Optional[Callable[[int, int], None]] = None,
                      workers: Optional[int] = None) -> TuneResult:
    """全部の録画での補正(tune)と、補正に使っていない録画での評価を合わせた結果。workers を
    省くと worker_count で決める。1 ならこのスレッドだけで計算する。"""
    native = native_module()  # 無ければ E506(記録を読む前に)
    samples = load_samples(db, scope_key, stop_event=stop_event)
    folds = holdout_folds(samples, scope_key)
    tasks = [("main", rows, scope_key, ())] + [("fold", rows, scope_key, tuple(f)) for f in folds]
    if workers is None:
        workers = worker_count(len(tasks))
    started = time.monotonic()
    done = 0
    results: List[tuple] = [None] * len(tasks)  # 仕事の順(合計の足す順を毎回同じにする)

    def report() -> None:
        if progress is not None:
            progress(done, len(tasks))

    report()

    def run_task(task) -> tuple:
        kind, task_rows, key, held = task
        if kind == "main":
            return kind, tune(samples, task_rows, key, stop_event=stop_event, log=False)
        return kind, run_fold(samples, task_rows, key, held, stop_event=stop_event)

    # Rust の評価に渡す録画は、並べる前にこのスレッドで作っておく(スレッドごとに作らないように)
    for s in samples:
        s.native_sample(native)
    if workers <= 1:
        for i, task in enumerate(tasks):
            results[i] = run_task(task)
            done += 1
            report()
    else:
        pool = concurrent.futures.ThreadPoolExecutor(workers, thread_name_prefix="autocut-tuning")
        try:
            pending = {pool.submit(run_task, task): i for i, task in enumerate(tasks)}
            while pending:
                finished, _ = concurrent.futures.wait(pending, timeout=0.1,
                                                      return_when=concurrent.futures.FIRST_COMPLETED)
                for f in finished:
                    results[pending.pop(f)] = f.result()  # 止めた時は TuningCancelled
                    done += 1
                if finished:
                    report()
                elif stop_event is not None and stop_event.is_set():
                    raise TuningCancelled()
        finally:
            # 動いている仕事は stop_event か例外で終わる。待たずに戻ると、止めた後も CPU を使い続ける
            pool.shutdown(wait=True, cancel_futures=True)
    main = next(r for kind, r in results if kind == "main")
    if folds:
        holdout = Score()
        for kind, score in results:
            if kind == "fold":
                holdout.add(score)
        main.holdout, main.folds = holdout, len(folds)
    logger.info("Tuned thresholds for %s: %d recordings, %d holdout folds, %d threads, %.1f s, changes %s",
                scope_key or "all stations", len(samples), len(folds), workers, time.monotonic() - started,
                {n: v for n, (_old, v) in main.changes.items()})
    return main
