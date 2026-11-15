"""局のロゴの変化の検知と作り直し・設定の「自動カット」タブ (設計書 §31.9、自動カット計画 A9)。

録画の型は .lgd を描いた型で作る (tests/test_autocut_lgd_import.py の write_lgd)。記録 DB・画面は本物。
"""

import io
import json

import numpy as np
import pytest

from tests.test_autocut_lgd_import import _ring_alpha, write_lgd
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.lgd_import import read_lgd, template_from_lgd
from ts_smartcut.autocut.logo import load_station_logo, restart_station_logo, update_station_logo
from ts_smartcut.autocut.logo_change import detect_logo_change, station_recordings, two_way_agreement

ONID, SID = 4, 211
OLD = (1080, 40)  # 前のロゴの位置 (1280×720 の右上)
NEW = (1120, 90)  # 変わった後のロゴ


def _template(tmp_path, *boxes):
    """boxes の位置に枠のロゴを描いた型 (いくつも描ける: 番組ごとの表示の代わり)。"""
    alpha = np.zeros((720, 1280), np.int16)
    for x, y in boxes:
        alpha[y:y + 70, x:x + 140] = _ring_alpha(140, 70, 10, 500)
    path = write_lgd(tmp_path / "t.lgd", 0, 0, alpha)
    return template_from_lgd(read_lgd(path)[0])


def _blob(template):
    """録画の特徴の BLOB のうち、録画の型の部分だけ (recording_template_from_features が読む所)。"""
    buf = io.BytesIO()
    meta = {"logo_source": "recording", "logo_strength": list(template.strength), "logo_frames": template.frames}
    np.savez_compressed(buf, logo_gx=np.asarray(template.gx), logo_gy=np.asarray(template.gy),
                        logo_mask_x=np.asarray(template.mask_x), logo_mask_y=np.asarray(template.mask_y), meta=np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8))
    return buf.getvalue()


def _add(db, template, day, program="番組"):
    """カットした録画 1 本 (その日の 01:00 の録画) と、その型の特徴を保存し、局の型を作り直す。"""
    start = f"2026-10-{day:02d}T01:00:00+09:00"
    with db.get_connection() as conn:
        rid = db.insert_recording(conn, {
            "file_name": f"{day}.ts", "file_size": 1, "file_hash": f"h{day}", "service_id": SID,
            "original_network_id": ONID, "service_name": "局", "program_name": program,
            "recording_start": start, "created_at": start, "updated_at": start,
        })
        db.insert_session(conn, {"recording_id": rid, "finalized_at": start, "processing_mode": "smart_cut",
                                 "source_type": "manual", "is_active": 1})
    db.save_features(rid, _blob(template), 3, True, 100)
    update_station_logo(db, rid)
    return rid


@pytest.fixture
def db(tmp_path):
    return CutHistoryDB(tmp_path / "cut_history.db")


def test_three_recordings_that_disagree_with_the_station_template_are_a_change(db, tmp_path):
    old, new = _template(tmp_path, OLD), _template(tmp_path, NEW)
    _add(db, old, 1)
    _add(db, old, 2)
    assert detect_logo_change(db, ONID, SID) is None
    ids = [_add(db, new, day) for day in (3, 4)]
    assert detect_logo_change(db, ONID, SID) is None  # まだ 2 本
    ids.append(_add(db, new, 5))
    change = detect_logo_change(db, ONID, SID)
    assert change is not None and change.recording_ids == ids
    assert change.since.startswith("2026-10-03") and change.latest.startswith("2026-10-05")
    assert all(a < 0.5 for a in change.agreements)


def test_one_recording_with_the_old_logo_in_between_is_not_a_change(db, tmp_path):
    old, new = _template(tmp_path, OLD), _template(tmp_path, NEW)
    for day, t in ((1, old), (2, old), (3, new), (4, old), (5, new)):
        _add(db, t, day)
    assert detect_logo_change(db, ONID, SID) is None


def test_programme_marks_next_to_the_logo_are_not_a_change(db, tmp_path):
    """録画の型に番組ごとの表示(別の隅のマーク)が入っても、局のロゴが残っていれば変わっていない。"""
    old = _template(tmp_path, OLD)
    with_mark = _template(tmp_path, OLD, (40, 600), (40, 40))
    assert two_way_agreement(with_mark, old) == pytest.approx(1.0)
    for day, t in ((1, old), (2, old), (3, with_mark), (4, with_mark), (5, with_mark)):
        _add(db, t, day)
    assert detect_logo_change(db, ONID, SID) is None


def test_no_is_remembered_until_three_newer_recordings(db, tmp_path):
    old, new = _template(tmp_path, OLD), _template(tmp_path, NEW)
    for day, t in ((1, old), (2, old), (3, new), (4, new), (5, new)):
        _add(db, t, day)
    change = detect_logo_change(db, ONID, SID)
    db.save_logo_change_dismissal(ONID, SID, change.since, change.latest)
    assert detect_logo_change(db, ONID, SID) is None
    _add(db, new, 6)
    _add(db, new, 7)
    assert detect_logo_change(db, ONID, SID) is None
    _add(db, new, 8)
    again = detect_logo_change(db, ONID, SID)
    assert again is not None and again.since.startswith("2026-10-06")


def test_restarting_from_the_change_builds_the_template_from_the_new_recordings(db, tmp_path):
    old, new = _template(tmp_path, OLD), _template(tmp_path, NEW)
    for day, t in ((1, old), (2, old), (3, new), (4, new), (5, new)):
        _add(db, t, day)
    change = detect_logo_change(db, ONID, SID)
    restart_station_logo(db, ONID, SID, change.since)
    station = load_station_logo(db, ONID, SID)
    assert two_way_agreement(new, station) == pytest.approx(1.0)
    assert detect_logo_change(db, ONID, SID) is None
    # 前の録画には前の型 (期間付きで残る)
    assert two_way_agreement(old, load_station_logo(db, ONID, SID, "2026-10-01T01:00:00+09:00")) == pytest.approx(1.0)
    # 作り直す録画を選ぶ一覧: 新しい順、今の局の型との一致
    listed = station_recordings(db, ONID, SID)
    assert [r.recorded_at[:10] for r in listed] == ["2026-10-05", "2026-10-04", "2026-10-03", "2026-10-02",
                                                    "2026-10-01"]
    assert listed[0].agreement == pytest.approx(1.0) and listed[-1].agreement < 0.5


def test_window_asks_and_restarts_or_remembers_no(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    db = window.history_db
    if db is None:
        pytest.skip("no history DB")
    old, new = _template(tmp_path, OLD), _template(tmp_path, NEW)
    for day, t in ((1, old), (2, old), (3, new), (4, new), (5, new)):
        _add(db, t, day)
    change = detect_logo_change(db, ONID, SID)
    asked = []

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    window._on_logo_change(window._program_info_request, (change, "BS 局"))
    assert len(asked) == 1 and "BS 局" in asked[0] and "2026-10-03" in asked[0]
    assert db.get_logo_change_dismissal(ONID, SID) == (change.since, change.latest)

    db.clear_logo_change_dismissal(ONID, SID)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._on_logo_change(window._program_info_request, (change, "BS 局"))
    assert db.get_station_logo(ONID, SID)["generation_start"] == change.since
    assert two_way_agreement(new, load_station_logo(db, ONID, SID)) == pytest.approx(1.0)
    window._on_logo_change(window._program_info_request - 1, (change, ""))  # 前に開いた録画の分は出さない
    assert len(asked) == 1


def test_settings_tab_shows_stations_saves_switches_and_rebuilds_a_logo(qapp, db, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from ts_smartcut.gui import autocut_settings_panel as panel_module
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    old, new = _template(tmp_path, OLD), _template(tmp_path, NEW)
    for day, t in ((1, old), (2, old), (3, new), (4, new), (5, new)):
        _add(db, t, day, program=f"番組{day}")
    dialog = SettingsDialog(AppSettings(), history_db=db)
    panel = dialog.autocut_panel
    assert panel.record_count_label.text() == "5 本"
    assert panel.station_table.rowCount() == 1
    assert panel.station_table.item(0, panel_module.COL_TEMPLATE).text() == "あり（5 本）"
    assert not panel.rebuild_logo_button.isEnabled()

    panel.station_table.selectRow(0)
    assert panel.rebuild_logo_button.isEnabled()
    offered = []

    def choose(parent, title, label, items, current, editable):
        offered.extend(items)
        return next(i for i in items if i.startswith("2026-10-03")), True

    monkeypatch.setattr(QInputDialog, "getItem", choose)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    panel._on_rebuild_logo()
    assert offered[0].startswith("2026-10-05") and "番組5" in offered[0] and "%" in offered[0]
    assert db.get_station_logo(ONID, SID)["generation_start"].startswith("2026-10-03")
    assert panel.station_table.item(0, panel_module.COL_TEMPLATE).text() == "あり（3 本）"

    panel.proposals_check.setChecked(False)
    panel.logo_change_check.setChecked(False)
    settings = dialog.get_settings()
    assert (settings.autocut_proposals, settings.notify_logo_change) == (False, False)


def test_settings_tab_without_history_db(qapp):
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    dialog = SettingsDialog(AppSettings(), history_db=None)
    panel = dialog.autocut_panel
    assert panel.station_table.rowCount() == 0
    assert not panel.open_folder_button.isEnabled() and not panel.rebuild_logo_button.isEnabled()


def test_proposal_switch_in_the_settings_stops_proposals(window, monkeypatch):
    monkeypatch.setattr(window, "autocut_proposals", True)
    window.settings.autocut_proposals = False
    try:
        assert not window._autocut_enabled()
    finally:
        window.settings.autocut_proposals = True
    assert window._autocut_enabled()
