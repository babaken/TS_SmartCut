"""しきい値の自動補正を自動で行い、自動で採用する (設計書 §31.9「しきい値の自動補正を自動で採用する」、
2026-10-15 利用者の決定)。

バッチが終わった後に裏のスレッドで、全局共通と、そのバッチで特徴を保存した録画の局(録画が
tuning.MIN_TUNE_RECORDINGS 本以上)を補正する。採用するのは、補正に使っていない録画での評価が今の値
より良く、その再現率が tuning.MAX_RECALL_DROP より下がらない時だけ(tuning.adoption_problem)。全局共通は
値を持つ局の値と組み合わさるので、どこかの局で値どうしの関係が崩れる時も採用しない。

全局共通を先に補正する(局の補正は全局共通の値の上で行うので、後の方が新しい値で計算できる)。
次のバッチが始まった時・アプリを閉じる時は中止する(cancel)。中止した局は次の回に回す。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Set

from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, Rows, consistency_error, resolve_detail, station_key
from ts_smartcut.autocut.tuning import (
    MIN_TUNE_RECORDINGS,
    TuneResult,
    TuningCancelled,
    adoption_problem,
    changes_to_rows,
    tune_with_holdout,
)

logger = logging.getLogger(__name__)


@dataclass
class AutoTuneOutcome:
    scope_key: str  # '' = 全局共通
    result: Optional[TuneResult]  # 録画が足りず計算しなかった時は None
    adopted: bool
    reason: Optional[str] = None  # 採用しなかった理由(ログ用の英語)


def stations_of_recordings(db, recording_ids: Iterable[int]) -> Set[str]:
    """録画の局の鍵。"""
    ids = sorted(set(recording_ids))
    if not ids:
        return set()
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT original_network_id, service_id FROM recordings WHERE id IN ({','.join('?' * len(ids))}) "
            "AND service_id IS NOT NULL;", ids).fetchall()
    return {station_key(r["original_network_id"], r["service_id"]) for r in rows}


def _inconsistent_station(rows: Rows, keys: Iterable[str]) -> Optional[str]:
    """値どうしの関係が崩れる局(全局共通は '')。無ければ None。"""
    for key in keys:
        values = {n: v for n, (v, _l, _s) in resolve_detail(rows, key, warn=False).items()}
        if consistency_error(DEFAULT_THRESHOLDS.replace(**values)) is not None:
            return key
    return None


def tune_and_adopt(db, scope_key: str, stop_event=None, workers: Optional[int] = None) -> AutoTuneOutcome:
    """scope_key の段を補正し、条件に合えば DB に書く。"""
    stations = db.list_stations()
    counts = {station_key(s["original_network_id"], s["service_id"]): s["recordings"] for s in stations}
    if scope_key and counts.get(scope_key, 0) < MIN_TUNE_RECORDINGS:
        return AutoTuneOutcome(scope_key, None, False, f"fewer than {MIN_TUNE_RECORDINGS} recordings")
    if not scope_key and not counts:
        return AutoTuneOutcome(scope_key, None, False, "no recordings")
    rows = db.get_threshold_rows()
    result = tune_with_holdout(db, rows, scope_key, stop_event=stop_event, workers=workers)
    problem = adoption_problem(result)
    if problem is None:
        new_rows = changes_to_rows(result)
        layered = {k: dict(v) for k, v in rows.items()}
        for (key, name), row in new_rows.items():
            layered.setdefault(key, {})[name] = row
        affected = [scope_key] if scope_key else [""] + sorted(counts)
        broken = _inconsistent_station(layered, affected)
        if broken is not None:
            problem = f"the values would be inconsistent for station {broken or 'all stations'!r}"
        else:
            db.save_threshold_changes(new_rows)
    scope = scope_key or "all stations"
    if problem is None:
        logger.info("Adopted tuned thresholds for %s: %s (recall on recordings left out %.1f%% -> %.1f%%)",
                    scope, {n: v for n, (_old, v) in result.changes.items()},
                    result.before.recall * 100, result.holdout.recall * 100)
        return AutoTuneOutcome(scope_key, result, True)
    logger.info("Did not adopt tuned thresholds for %s: %s", scope, problem)
    return AutoTuneOutcome(scope_key, result, False, problem)


def run_auto_tuning(db, station_keys: Iterable[str], stop_event=None, workers: Optional[int] = None,
                    on_outcome: Optional[Callable[[AutoTuneOutcome], None]] = None) -> List[AutoTuneOutcome]:
    """全局共通と station_keys の局を順に補正する。中止すると TuningCancelled。"""
    outcomes = []
    for key in [""] + sorted(k for k in set(station_keys) if k):
        if stop_event is not None and stop_event.is_set():
            raise TuningCancelled()
        outcome = tune_and_adopt(db, key, stop_event=stop_event, workers=workers)
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
    return outcomes


class AutoTuner:
    """裏のスレッドで run_auto_tuning を 1 回ずつ動かす。中止した回の局は次の start に回す。
    on_finished(採用した AutoTuneOutcome の一覧) は裏のスレッドから呼ばれる。"""

    def __init__(self, db, on_finished: Optional[Callable[[List[AutoTuneOutcome]], None]] = None,
                 workers: Optional[int] = None):
        self._db = db
        self._on_finished = on_finished
        self._workers = workers
        self._lock = threading.Lock()
        self._pending: Set[str] = set()  # まだ補正していない局(中止した回の局も)
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, station_keys: Sequence[str] = ()) -> bool:
        """補正を始める。動いている間は False(局は次の回に回す)。"""
        with self._lock:
            self._pending |= set(station_keys)
            if self._thread is not None and self._thread.is_alive():
                return False
            keys, self._pending = self._pending, set()
            stop = self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(keys, stop), name="auto-tuning", daemon=True)
            self._thread.start()
            return True

    def cancel(self, wait: float = 0.0) -> None:
        """動いている補正をやめる(局は次の回に回す)。wait 秒まで終わるのを待つ。"""
        with self._lock:
            stop, thread = self._stop, self._thread
        if stop is not None:
            stop.set()
        if thread is not None and wait > 0:
            thread.join(wait)

    def _run(self, keys: Set[str], stop: threading.Event) -> None:
        adopted: List[AutoTuneOutcome] = []
        done: Set[str] = set()
        logger.info("Started automatic threshold tuning: all stations and %d stations", len(keys))

        def on_outcome(outcome: AutoTuneOutcome) -> None:
            done.add(outcome.scope_key)
            if outcome.adopted:
                adopted.append(outcome)

        try:
            run_auto_tuning(self._db, keys, stop_event=stop, workers=self._workers, on_outcome=on_outcome)
        except TuningCancelled:
            logger.info("Cancelled automatic threshold tuning")
            with self._lock:
                self._pending |= keys - done  # 全局共通は毎回補正する
        except Exception:  # noqa: BLE001 - 自動補正の失敗で他の操作を止めない
            logger.warning("Automatic threshold tuning failed", exc_info=True)
        if adopted and self._on_finished is not None:
            self._on_finished(adopted)
