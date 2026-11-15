"""ルールのしきい値と自動補正 (設計書 §31.9「ルールのしきい値」、自動カット計画「A8 の後の作業」1)。

- 値の段: 局の値 → 全局共通の値 → 初期値。範囲外・崩れた組み合わせは使わない。
- 記録 DB 版 8 の thresholds(移行・読み書き)。
- 判定が値を使うこと(無音の音量を変えると境目が見つかる)、評価ツールの初期値の指定。
- 自動補正: 正解に合う値を選ぶ・手動の値は動かさない。
- 自動で採用する条件(2026-10-15): 使っていない録画で良くなり、再現率が 5 ポイント以上は下がらない時だけ。
  バッチの後に裏で補正する AutoTuner(中止した局は次の回へ)。
- 設定の「しきい値」: 一覧だけ(値・使っている値と自動補正の日時)。
"""

import sqlite3

import numpy as np
import pytest

from tests.test_autocut_rules import PARTS, _features, _keep, _record
from ts_smartcut.autocut.candidates import find_boundaries
from ts_smartcut.autocut.evaluate import evaluate_all
from ts_smartcut.autocut.history_db import CURRENT_SCHEMA_VERSION, CutHistoryDB
from ts_smartcut.autocut.thresholds import (
    AUTO,
    DEFAULT,
    DEFAULT_THRESHOLDS,
    GLOBAL,
    MANUAL,
    SPEC_BY_NAME,
    SPECS,
    STATION,
    ThresholdRow,
    load_thresholds,
    resolve,
    resolve_detail,
    station_key,
)
from ts_smartcut.autocut.auto_tuning import AutoTuner, run_auto_tuning, stations_of_recordings, tune_and_adopt
from ts_smartcut.autocut.evaluate import Score
from ts_smartcut.autocut.tuning import (
    MAX_RECALL_DROP,
    TuneResult,
    TuningCancelled,
    adoption_problem,
    changes_to_rows,
    holdout_folds,
    load_samples,
    objective,
    run_fold,
    tune,
    tune_with_holdout,
)

KEY = station_key(4, 211)  # tests.test_autocut_rules._record の局


def _quiet_features(seed=0):
    """境目の無音が −70dB(初期値の −80dB では無音にならない)の録画。"""
    f, spans = _features(PARTS, seed=seed)
    level = np.asarray(f.audio_level)
    f.audio_level = np.where(level == 0, 60, level).astype(np.uint8)  # 60 → −70dB
    return f, spans


# ---- 値の定義と段 ---------------------------------------------------------------

def test_every_threshold_has_a_spec_and_defaults_are_in_range():
    assert [s.name for s in SPECS] == list(DEFAULT_THRESHOLDS.as_dict())
    for spec in SPECS:
        assert spec.accepts(spec.default), spec.name
        grid = spec.grid()
        assert grid[0] == pytest.approx(spec.minimum) and grid[-1] == pytest.approx(spec.maximum)


def test_station_value_then_all_stations_then_default():
    rows = {
        "": {"silence_db": ThresholdRow(-70.0, MANUAL), "scene_min": ThresholdRow(20.0, AUTO)},
        KEY: {"silence_db": ThresholdRow(-60.0, AUTO)},
    }
    detail = resolve_detail(rows, KEY)
    assert detail["silence_db"] == (-60.0, STATION, AUTO)
    assert detail["scene_min"] == (20.0, GLOBAL, AUTO)
    assert detail["min_silence"] == (DEFAULT_THRESHOLDS.min_silence, DEFAULT, None)
    assert resolve(rows, KEY).silence_db == -60.0
    assert resolve(rows, "4:999").silence_db == -70.0  # 局の値の無い局は全局共通
    assert resolve(rows, "").silence_db == -70.0


def test_out_of_range_and_unknown_rows_are_ignored():
    rows = {"": {"silence_db": ThresholdRow(-10.0, MANUAL), "future_value": ThresholdRow(1.0, AUTO)}}
    assert resolve(rows, KEY).silence_db == DEFAULT_THRESHOLDS.silence_db


def test_inconsistent_combination_falls_back_to_the_defaults():
    # 局の CM の値が、全局共通の本編の値より大きくなった
    rows = {"": {"logo_program": ThresholdRow(0.3, MANUAL)}, KEY: {"logo_cm": ThresholdRow(0.4, MANUAL)}}
    assert resolve(rows, KEY) == DEFAULT_THRESHOLDS
    assert resolve(rows, "").logo_program == 0.3


# ---- 記録 DB --------------------------------------------------------------------

def test_history_db_v7_migrates_to_v8_with_an_empty_thresholds_table(tmp_path):
    path = tmp_path / "cut_history.db"
    db = CutHistoryDB(path)
    with db.get_connection() as conn:  # 版 7 の DB にする
        conn.execute("DROP TABLE thresholds;")
        conn.execute("DELETE FROM schema_info WHERE version >= 8;")
    CutHistoryDB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM thresholds").fetchone()[0] == 0
    conn.close()


def test_threshold_rows_are_saved_and_removed(tmp_path):
    db = CutHistoryDB(tmp_path / "cut_history.db")
    db.save_threshold_changes({(KEY, "silence_db"): ThresholdRow(-60.0, MANUAL),
                               ("", "scene_min"): ThresholdRow(20.0, AUTO)})
    assert db.get_threshold_rows() == {KEY: {"silence_db": ThresholdRow(-60.0, MANUAL)},
                                       "": {"scene_min": ThresholdRow(20.0, AUTO)}}
    with db.get_connection() as conn:
        row = conn.execute("SELECT original_network_id, service_id FROM thresholds WHERE station_key = ?",
                           (KEY,)).fetchone()
    assert tuple(row) == (4, 211)
    assert load_thresholds(db, 4, 211).silence_db == -60.0
    db.save_threshold_changes({(KEY, "silence_db"): None})
    assert load_thresholds(db, 4, 211).silence_db == DEFAULT_THRESHOLDS.silence_db


# ---- 判定と評価 -----------------------------------------------------------------

def test_boundaries_follow_the_silence_threshold():
    f, spans = _quiet_features()
    assert find_boundaries(f) == []
    found = find_boundaries(f, DEFAULT_THRESHOLDS.replace(silence_db=-60.0))
    assert [b.frame for b in found] == [a for _k, a, _b in spans[1:]]


def test_proposals_and_evaluation_use_the_history_db_values(tmp_path):
    db = CutHistoryDB(tmp_path / "cut_history.db")
    f, spans = _quiet_features()
    _record(db, f, _keep(spans))
    assert evaluate_all(db)[0].score.recall < 0.1  # 初期値では CM が見つからない
    db.save_threshold_changes({(KEY, "silence_db"): ThresholdRow(-60.0, MANUAL)})
    assert evaluate_all(db)[0].score.recall == pytest.approx(1.0, abs=0.01)
    assert evaluate_all(db, defaults=True)[0].score.recall < 0.1  # 比べるための初期値の評価


# ---- 自動補正 -------------------------------------------------------------------

def _two_quiet_recordings(tmp_path, other_station=0):
    """KEY の局の静かな録画 2 本(と、other_station 本の別の局 4:212 の録画)。"""
    db = CutHistoryDB(tmp_path / "cut_history.db")
    for seed, start in ((0, "2026-10-01T00:59:56+09:00"), (1, "2026-10-08T00:59:56+09:00")):
        f, spans = _quiet_features(seed)
        _record(db, f, _keep(spans), start=start)
    for k in range(other_station):
        f, spans = _quiet_features(10 + k)
        _record(db, f, _keep(spans), service_id=212, start=f"2026-10-0{k + 2}T00:59:56+09:00")
    return db


def test_tuning_finds_the_silence_level_of_the_station(tmp_path):
    db = _two_quiet_recordings(tmp_path)
    samples = load_samples(db, KEY)
    assert len(samples) == 2
    result = tune(samples, {}, KEY)
    assert result.before.recall < 0.1
    assert result.after.recall == pytest.approx(1.0, abs=0.01) and result.after.wrong_cut == 0
    assert -70.0 < result.changes["silence_db"][1] <= SPEC_BY_NAME["silence_db"].maximum
    assert changes_to_rows(result)[(KEY, "silence_db")].source == AUTO


def test_tuning_never_moves_manual_values(tmp_path):
    db = _two_quiet_recordings(tmp_path)
    rows = {"": {"silence_db": ThresholdRow(-80.0, MANUAL)}}
    result = tune(load_samples(db, ""), rows, "")
    assert "silence_db" not in result.changes


# ---- 設定の「しきい値」(一覧だけ) ------------------------------------------------

def _shown(name: str, value: float) -> str:
    return f"{value:.{SPEC_BY_NAME[name].decimals}f}"


def test_threshold_panel_lists_the_values_in_use(qapp, tmp_path):
    from ts_smartcut.gui.threshold_panel import ThresholdPanel

    db = _two_quiet_recordings(tmp_path, other_station=1)
    db.save_threshold_changes({(KEY, "silence_db"): ThresholdRow(-60.0, AUTO),
                               ("", "scene_min"): ThresholdRow(20.0, AUTO)})
    panel = ThresholdPanel(db)
    assert panel.station_combo.count() == 3  # 全局共通 + 2 局
    assert not hasattr(panel, "tune_button")  # 手入力・戻す・補正のボタンは無い(2026-10-15)
    assert panel.value_text("silence_db") == _shown("silence_db", -80.0)
    assert panel.value_text("scene_min") == _shown("scene_min", 20.0)
    assert panel.source_text("scene_min").startswith("全局共通（自動補正 2")  # 補正した日時を出す
    assert panel.source_text("silence_db") == "初期値"
    keys = [panel.station_combo.itemData(i)[0] for i in range(3)]
    panel.station_combo.setCurrentIndex(keys.index(KEY))
    assert panel.value_text("silence_db") == _shown("silence_db", -60.0)
    assert panel.source_text("silence_db").startswith("この局（自動補正 2")
    assert panel.value_text("scene_min") == _shown("scene_min", 20.0)  # 局の値が無い項目は全局共通
    panel.station_combo.setCurrentIndex(keys.index(station_key(4, 212)))
    assert "2 本未満" in panel.hint_label.text()  # 1 本の局は補正しない


def test_settings_dialog_opens_with_and_without_history_db(qapp, tmp_path):
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    db = _two_quiet_recordings(tmp_path)
    dialog = SettingsDialog(AppSettings(), history_db=db)
    dialog.accept()
    assert db.get_threshold_rows() == {}  # 設定の OK では書かない
    dialog = SettingsDialog(AppSettings())
    assert dialog.threshold_panel.station_combo is None
    dialog.accept()


# ---- 自動で採用する(2026-10-15) ----------------------------------------------------

def _result(before: Score, holdout, changes=None) -> TuneResult:
    return TuneResult("", 2, before, before, {"silence_db": (-80.0, -60.0)} if changes is None else changes,
                      holdout=holdout, folds=2 if holdout is not None else 0)


def test_adoption_needs_a_better_holdout_without_a_large_recall_drop():
    before = Score(truth_cut=100.0, proposed_cut=60.0, correct_cut=60.0, wrong_cut=0.0)
    better = Score(truth_cut=100.0, proposed_cut=80.0, correct_cut=80.0, wrong_cut=0.0)
    assert adoption_problem(_result(before, better)) is None
    assert adoption_problem(_result(before, better, changes={})) == "no change"
    assert "left out" in adoption_problem(_result(before, None))  # 確かめる録画が無い
    worse = Score(truth_cut=100.0, proposed_cut=50.0, correct_cut=40.0, wrong_cut=10.0)
    assert "not better" in adoption_problem(_result(before, worse))
    # 本編を誤って切るのをやめた分、良さは上がるが、CM をほとんど切らなくなる補正(10/14 の CS-F)
    cutting = Score(truth_cut=100.0, proposed_cut=61.0, correct_cut=58.0, wrong_cut=3.0)
    nothing = Score(truth_cut=100.0, proposed_cut=30.0, correct_cut=30.0, wrong_cut=0.0)
    assert objective(nothing) > objective(cutting)
    assert "recall" in adoption_problem(_result(cutting, nothing))
    # 5 ポイントまでの下がり方は採用する
    slight = Score(truth_cut=100.0, proposed_cut=58.0 - MAX_RECALL_DROP * 100 + 1, wrong_cut=0.0,
                   correct_cut=58.0 - MAX_RECALL_DROP * 100 + 1)
    assert adoption_problem(_result(cutting, slight)) is None


def test_tune_and_adopt_writes_only_an_adoptable_result(tmp_path):
    db = _two_quiet_recordings(tmp_path)
    outcome = tune_and_adopt(db, KEY, workers=1)
    assert outcome.adopted, outcome.reason
    assert db.get_threshold_rows()[KEY]["silence_db"].source == AUTO
    assert load_thresholds(db, 4, 211).silence_db > -70.0
    # 全局共通: 局が 1 つだけでは、補正に使っていない録画で確かめられないので採用しない
    db.save_threshold_changes({(KEY, "silence_db"): None})
    outcome = tune_and_adopt(db, "", workers=1)
    assert outcome.result.changes and not outcome.adopted and "left out" in outcome.reason
    assert "" not in db.get_threshold_rows()


def test_stations_with_too_few_recordings_are_not_tuned(tmp_path):
    db = _two_quiet_recordings(tmp_path, other_station=1)
    outcome = tune_and_adopt(db, station_key(4, 212), workers=1)
    assert outcome.result is None and not outcome.adopted


def test_run_auto_tuning_does_all_stations_first(tmp_path):
    db = _two_quiet_recordings(tmp_path, other_station=1)
    ids = [s.recording_id for s in load_samples(db, "")]
    assert stations_of_recordings(db, ids) == {KEY, station_key(4, 212)}
    outcomes = run_auto_tuning(db, [KEY], workers=1)
    assert [o.scope_key for o in outcomes] == ["", KEY]
    stop = __import__("threading").Event()
    stop.set()
    with pytest.raises(TuningCancelled):
        run_auto_tuning(db, [KEY], stop_event=stop, workers=1)


def test_auto_tuner_reports_adopted_results_and_retries_cancelled_stations(tmp_path):
    import threading

    db = _two_quiet_recordings(tmp_path)
    finished = []
    done = threading.Event()
    tuner = AutoTuner(db, on_finished=lambda outcomes: (finished.append(outcomes), done.set()), workers=1)
    assert tuner.start([KEY])
    assert done.wait(120)
    tuner._thread.join(10)
    assert not tuner.is_busy
    assert [o.scope_key for o in finished[0]] == [KEY]  # 採用した局だけ(全局共通は確かめられず見送り)

    db.save_threshold_changes({(KEY, "silence_db"): None})
    tuner = AutoTuner(db, workers=1)
    tuner.start([KEY])
    tuner.cancel(wait=120)  # 始めてすぐ中止 → 局は次の回に回す
    assert not tuner.is_busy
    assert tuner._pending in ({KEY}, set())  # 中止が間に合わず終わった時は空
    if tuner._pending:
        assert "silence_db" not in db.get_threshold_rows().get(KEY, {})


# ---- 補正に使っていない録画での評価 -------------------------------------------------

def test_holdout_folds_leave_out_one_recording_or_one_station(tmp_path):
    db = _two_quiet_recordings(tmp_path, other_station=1)
    station = load_samples(db, KEY)
    assert holdout_folds(station, KEY) == [[s.recording_id] for s in station]
    everything = load_samples(db, "")
    folds = holdout_folds(everything, "")
    assert sorted(len(f) for f in folds) == [1, 2]  # 局 4:212 の 1 本と、KEY の局の 2 本
    assert holdout_folds(load_samples(db, station_key(4, 212)), station_key(4, 212)) == []


def test_fold_tunes_without_the_left_out_recording(tmp_path):
    db = _two_quiet_recordings(tmp_path)
    samples = load_samples(db, KEY)
    held = samples[0].recording_id
    score = run_fold(samples, {}, KEY, [held])
    assert score.recordings == 1  # 外した録画だけを数える
    assert score.recall == pytest.approx(1.0, abs=0.01)  # もう 1 本で見つけた無音の値が効く


def test_holdout_worse_warns_when_left_out_recordings_get_worse(tmp_path):
    db = _two_quiet_recordings(tmp_path)
    result = tune_with_holdout(db, {}, KEY, workers=1)
    assert result.folds == 2 and result.holdout is not None
    assert result.holdout_worse is False  # numpy の bool ではなく bool(ログ・JSON に出せる)
    worse = result.holdout.__class__(truth_cut=100.0, proposed_cut=50.0, correct_cut=0.0, wrong_cut=50.0)
    result.holdout = worse
    assert objective(worse) < objective(result.before) and result.holdout_worse is True
