"""学習による判定 (自動カット計画 A8) と、設定の「学習」タブ・局ごとの方式 (A9 の残り)。

ルールでは当たらない区間を持つ合成の録画で確かめる: CM の間に挟まった、ロゴの出る 45 秒の番宣
(ルールはロゴを見て本編にする)。利用者は毎回これを切っている。記録 DB・学習 (Rust の GbdtModel)・画面は本物。
学習器そのものは test_gbdt.py。
"""

import json
import time

import numpy as np
import pytest

from tests.test_autocut_rules import FPS, _features, _keep, _record  # noqa: E402
from ts_smartcut.autocut import model as learning  # noqa: E402
from ts_smartcut.autocut.history_db import CutHistoryDB  # noqa: E402
from ts_smartcut.autocut.proposals import build_file_proposals  # noqa: E402
from ts_smartcut.autocut.rules import CM, PROGRAM, StationProfile  # noqa: E402
from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, station_key  # noqa: E402

ONID, SID = 4, 211
KEY = station_key(ONID, SID)


def _parts(rng):
    """番組の前後・本編・CM と、CM に挟まれたロゴ付きの番宣(promo)。長さは録画ごとに少し変える。"""
    a, b, c = (int(x) for x in rng.integers(-5, 6, 3))
    return [
        ("margin", 5), ("program", 200 + a), ("cm", 15), ("promo", 45), ("cm", 15),
        ("program", 400 + b), ("cm", 30), ("cm", 30), ("program", 300 + c), ("cm", 60), ("margin", 25),
    ]


def _recording(seed):
    rng = np.random.default_rng(seed)
    features, spans = _features(_parts(rng), seed=seed)
    score = np.asarray(features.logo_score)
    for kind, a, b in spans:
        if kind == "promo":
            score[1, a:b] = 200  # 局のロゴが出たままの番宣
    features.logo_score = score
    return features, spans


def _fill(db, count=24):
    """1 日 1 本の録画 count 本(古い順)。利用者は本編だけを残し、CM の直前の 1 フレームを残さない。"""
    ids = []
    for k in range(count):
        features, spans = _recording(k)
        keep = [(a, b - 1) for a, b in _keep(spans)]
        ids.append(_record(db, features, keep, start=f"2026-09-{k + 1:02d}T00:59:56+09:00"))
    return ids


@pytest.fixture
def db(tmp_path):
    return CutHistoryDB(tmp_path / "cut_history.db")


def test_segment_table_has_one_row_of_features_per_segment_and_labels_from_the_cuts():
    features, spans = _recording(0)
    table = learning.segment_table(features, None, StationProfile(), DEFAULT_THRESHOLDS, 0.0, "bs", 0x70)
    assert table.x.shape == (len(table.segments), len(learning.FEATURE_NAMES))
    keep = [(a, b, False) for a, b in _keep(spans)]
    cut, cut_s, keep_s, use = learning.segment_labels(table, keep, features.fps)
    assert all(use)
    promo = next(i for i, s in enumerate(table.segments) if abs(s.seconds - 45) < 1)
    assert cut[promo] and keep_s[promo] == 0
    # ルールはロゴを見て、番宣を本編にする
    assert table.rule_decisions[promo].kind == PROGRAM
    longest = int(np.argmax([s.seconds for s in table.segments]))
    assert not cut[longest] and cut_s[longest] == 0
    column = learning.FEATURE_NAMES.index("seconds")
    assert table.x[longest, column] == pytest.approx(table.segments[longest].seconds)


def test_learning_beats_the_rules_on_the_newest_recordings(db):
    _fill(db)
    items = learning.load_items(db)
    assert [it.recorded_at for it in items] == sorted(it.recorded_at for it in items)
    result = learning.train_and_evaluate(items)
    assert result.eval_recordings == 6 and result.train_recordings == 18
    # ルールは番宣を残す(再現率が下がる)。学習は切る
    assert result.rule.recall < 0.85
    assert result.learned.recall > result.rule.recall + 0.1
    assert result.learned.wrong_cut <= result.rule.wrong_cut + 1.0
    assert result.adoption_problem() is None
    assert result.stations[KEY].eval_recordings == 6

    # 保存して読み直しても同じ判定
    again = learning.LearnedModel.from_blob(result.model.to_blob())
    features, _spans = _recording(99)
    table = learning.segment_table(features, None, StationProfile(), DEFAULT_THRESHOLDS,
                                   result.model.station_code(KEY), "bs", None)
    assert np.allclose(again.predict_cut(table.x), result.model.predict_cut(table.x))
    decisions = learning.learned_decisions(again, table)
    promo = next(d for d in decisions if abs(d.segment.seconds - 45) < 1)
    assert promo.kind == CM and promo.rule == "learned"


def test_retrain_saves_the_result_and_proposals_use_it_when_chosen(db):
    ids = _fill(db)
    assert learning.load_active_model(db) is None
    result = learning.retrain(db)
    row = db.get_latest_model(active_only=True)
    assert row is not None and row["is_active"] == 1
    assert row["recordings_count"] == len(ids) and row["eval_recordings_count"] == result.eval_recordings
    assert row["library"] == learning.LIBRARY == "native-gbdt"
    assert row["feature_version"] == learning.MODEL_FEATURE_VERSION
    details = json.loads(row["details"])
    assert details["stations"][KEY]["eval"] == 6
    assert learning.station_problem(details, KEY) is None

    class Event:
        def __init__(self, start):
            from datetime import timedelta

            self.start, self.duration, self.genres = start, timedelta(seconds=1100), [0x70]

    class Info:
        from datetime import datetime, timedelta

        recording_start = datetime(2026, 10, 1, 0, 59, 56)
        event = Event(recording_start + timedelta(seconds=4))
        service_id, original_network_id, broadcast_type = SID, ONID, "bs"

    features, spans = _recording(7)
    registration = {"id": ids[7]}
    rule = build_file_proposals(db, None, FPS, Info(), registration, default_method="rule")
    learned = build_file_proposals(db, None, FPS, Info(), registration, default_method="learned")
    promo = next((a, b) for kind, a, b in spans if kind == "promo")

    def kept(proposals, a, b):
        return sum(max(0, min(p.end_frame, b) - max(p.start_frame, a)) for p in proposals)

    assert kept(rule, *promo) > 0  # ルールは番宣を残す
    assert kept(learned, *promo) == 0  # 学習は切る
    assert any("学習" in p.reason for p in learned)
    # 局ごとの方式は全体の既定より強い
    by_station = build_file_proposals(db, None, FPS, Info(), registration, default_method="learned",
                                      station_methods={KEY: "rule"})
    assert [(p.start_frame, p.end_frame) for p in by_station] == [(p.start_frame, p.end_frame) for p in rule]

    assert db.clear_models() >= 1
    assert learning.load_active_model(db) is None
    after_clear = build_file_proposals(db, None, FPS, Info(), registration, default_method="learned")
    assert [(p.start_frame, p.end_frame) for p in after_clear] == [(p.start_frame, p.end_frame) for p in rule]


def test_a_result_worse_than_the_rules_is_saved_but_not_used(db, monkeypatch):
    _fill(db, 12)
    monkeypatch.setattr(learning.TrainingResult, "adoption_problem", lambda self: "not better")
    learning.retrain(db)
    assert db.get_latest_model()["is_active"] == 0
    assert db.get_latest_model(active_only=True) is None
    assert learning.load_active_model(db) is None


def test_station_problems_and_chosen_method():
    score = {"correct_cut": 100.0, "wrong_cut": 0.0, "near_edges": 4}
    worse = {"correct_cut": 50.0, "wrong_cut": 0.0, "near_edges": 4}
    details = {"stations": {
        "4:1": {"train": 5, "eval": 2, "learned": score, "rule": worse},
        "4:2": {"train": 1, "eval": 0, "learned": score, "rule": score},
        "4:3": {"train": 5, "eval": 2, "learned": worse, "rule": score},
    }}
    assert learning.station_problem(details, "4:1") is None
    assert learning.station_problem(details, "4:2") == learning.REASON_FEW_RECORDINGS
    assert learning.station_problem(details, "9:9") == learning.REASON_FEW_RECORDINGS
    assert learning.station_problem(details, "4:3") == learning.REASON_RULE_BETTER
    assert learning.station_problem(None, "4:1") == learning.REASON_NO_MODEL
    assert learning.chosen_method("learned", {}, "4:1") == "learned"
    assert learning.chosen_method("learned", {"4:1": "rule"}, "4:1") == "rule"
    assert learning.chosen_method("rule", {"4:1": "learned"}, "4:1") == "learned"
    assert learning.chosen_method("rule", {"4:1": "default"}, "4:1") == "rule"


def test_without_the_native_module_proposals_use_the_rules(db, monkeypatch):
    _fill(db, 12)
    learning.retrain(db)
    monkeypatch.setattr(learning, "learning_available", lambda: False)
    assert learning.load_active_model(db) is None


def test_a_model_from_the_previous_library_is_not_used(qapp, db):
    """2026-10-30 より前の LightGBM のモデルは使わず、学習し直すまでルール。画面にもそう出す。"""
    import zlib

    from ts_smartcut.gui.autocut_settings_panel import AutoCutSettingsPanel

    payload = {"booster": "tree\nversion=v4\n", "station_codes": {KEY: 0}, "cut_threshold": 0.5,
               "feature_version": learning.MODEL_FEATURE_VERSION, "feature_names": learning.FEATURE_NAMES}
    details = {"rule": {}, "learned": {}, "stations": {}}
    db.save_model("lightgbm", "4.7.0", learning.MODEL_FEATURE_VERSION, 24, 300, 0, None, None, json.dumps(details),
                  True, zlib.compress(json.dumps(payload).encode("utf-8")))
    assert learning.load_active_model(db) is None
    with pytest.raises(ValueError):
        learning.LearnedModel.from_blob(zlib.compress(json.dumps(payload).encode("utf-8")))
    text = AutoCutSettingsPanel.status_text(db.get_latest_model(), True)
    assert "前の学習のライブラリで作った結果なので使っていません" in text


def _wait(qapp, condition, seconds=120):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert condition()


def test_learning_tab_retrains_clears_and_saves_the_methods(qapp, db, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.gui import autocut_settings_panel as panel_module
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    _fill(db)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: shown.append(text))
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: shown.append(text))
    dialog = SettingsDialog(AppSettings(), history_db=db)
    panel = dialog.autocut_panel
    assert panel.learning_status_label.text().startswith("学習の結果はありません")
    assert panel.retrain_button.isEnabled() and not panel.clear_learning_button.isEnabled()
    assert panel.station_table.item(0, panel_module.COL_ACCURACY).text() == "—"

    panel._on_retrain()
    assert panel.retraining() and not panel.retrain_button.isEnabled()
    _wait(qapp, lambda: not panel.retraining())
    assert shown and shown[-1].startswith("学習し直しました。次に開いた録画の案から使います。")
    assert "この結果で案を作っています" in panel.learning_status_label.text()
    assert "（6 本）" in panel.station_table.item(0, panel_module.COL_ACCURACY).text()
    assert panel.clear_learning_button.isEnabled()

    # 方式: 既定はルール、局は学習
    panel.method_combo.setCurrentIndex(0)
    combo = panel.station_table.cellWidget(0, panel_module.COL_METHOD)
    combo.setCurrentIndex(combo.findData("learned"))
    settings = dialog.get_settings()
    assert settings.autocut_method == "rule" and settings.autocut_station_methods == {KEY: "learned"}

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel._on_clear_learning()
    assert db.get_latest_model() is None
    assert panel.learning_status_label.text().startswith("学習の結果はありません")
    # 表を作り直しても選んだ方式は残る
    combo = panel.station_table.cellWidget(0, panel_module.COL_METHOD)
    assert combo.currentData() == "learned"


def test_learning_tab_cancel_keeps_the_previous_result(qapp, db, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    _fill(db, 12)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: shown.append(text))
    original = learning.load_items

    def slow_load(db_, stop_event=None, progress=None):
        while not stop_event.is_set():
            time.sleep(0.01)
        return original(db_, stop_event, progress)

    monkeypatch.setattr(learning, "load_items", slow_load)
    dialog = SettingsDialog(AppSettings(), history_db=db)
    panel = dialog.autocut_panel
    panel._on_retrain()
    panel.stop_retraining()
    _wait(qapp, lambda: not panel.retraining())
    assert shown[-1] == "学習を中止しました。前の結果のままです。"
    assert db.get_latest_model() is None


def test_learning_tab_without_the_native_module_or_history_db(qapp, monkeypatch):
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    monkeypatch.setattr(learning, "learning_available", lambda: False)
    panel = SettingsDialog(AppSettings(), history_db=None).autocut_panel
    assert panel.learning_status_label.text().startswith("学習に使う Rust の部品が入っていない")
    assert not panel.retrain_button.isEnabled() and not panel.clear_learning_button.isEnabled()
