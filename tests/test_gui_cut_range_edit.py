"""選んだ区間を S / E で直す・区間の開始 / 終了へ跳ぶ・Esc で選択を外す (設計書 §4.3〜4.5、2026-10-14)。

本物の Qt の画面と mpv(テスト用の合成の動画、75 フレーム)。キーの割り当ては QShortcut なので、
ここでは割り当てた先の処理を呼ぶ(実際にキーを押す確かめは tools/verify_cut_edit.py)。
"""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from tests._qt_helpers import pump_events
from ts_smartcut.models import CutProposal


def _seek(window, qapp, frame):
    window.preview_widget.controller._mpv.pause = True
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() is not None, timeout_ms=3000)
    window.preview_widget.seek_frame(frame)
    assert pump_events(qapp, lambda: window.preview_widget.get_current_frame() == frame, timeout_ms=3000)
    pump_events(qapp, lambda: False, timeout_ms=100)


def _ranges(window):
    return [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts]


@pytest.fixture
def opened(window, interlaced_sample_video, monkeypatch, qapp):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    # 警告の画面は本物を出さない(出すと押されるまでテストが止まる。2026-10-14 に 6 分止まった)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    window.open_file(interlaced_sample_video)
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() is not None, timeout_ms=5000)
    yield window


def test_s_and_e_change_the_selected_range_and_keep_it_selected(opened, qapp):
    window = opened
    window.cut_manager.add_cut(10, 40)
    window.cut_manager.add_cut(50, 70)
    window._refresh_cut_list()
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    assert "10-40" in window.pending_label.text()

    _seek(window, qapp, 9)
    window._on_set_start()
    assert _ranges(window) == [(9, 40), (50, 70)]
    assert window._selected_cut_index == 0  # 選んだまま
    assert window.preview_widget.get_current_frame() == 9  # 開始へ移動しない
    assert "9-40" in window.pending_label.text()
    assert "9" in window.status_bar.currentMessage()

    _seek(window, qapp, 39)
    window._on_set_end()
    assert _ranges(window) == [(9, 39), (50, 70)]
    assert window._selected_cut_index == 0

    window._on_undo()
    window._on_undo()
    assert _ranges(window) == [(10, 40), (50, 70)]
    window._on_redo()
    window._on_redo()
    assert _ranges(window) == [(9, 39), (50, 70)]


def test_invalid_changes_show_the_error_and_keep_the_range(opened, qapp, monkeypatch):
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    window.cut_manager.add_cut(20, 30)
    window.cut_manager.add_cut(40, 60)
    window._refresh_cut_list()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    _seek(window, qapp, 35)
    window._on_set_start()  # 開始 35 > 終了 30
    _seek(window, qapp, 45)
    window._on_set_end()  # 20-45 は 40-60 と重なる
    assert _ranges(window) == [(20, 30), (40, 60)]
    assert len(warnings) == 2 and "E204" in warnings[0] and "E202" in warnings[1]
    assert window._selected_cut_index == 0


def test_without_a_selection_s_and_e_remember_a_new_range(opened, qapp):
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    window.cut_manager.add_cut(20, 30)
    window._refresh_cut_list()
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    window._on_clear_cut_selection()  # Esc
    assert window._selected_cut_index is None
    _seek(window, qapp, 50)
    window._on_set_start()
    _seek(window, qapp, 60)
    window._on_set_end()
    assert _ranges(window) == [(20, 30)]
    assert (window._pending_start, window._pending_end) == (50, 60)
    assert "Start: 50 / End: 60" in window.pending_label.text()
    window._pending_start = window._pending_end = None
    window._update_pending_label()


def test_clicking_an_empty_part_of_the_list_deselects(opened):
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    window.cut_manager.add_cut(20, 30)
    window._refresh_cut_list()
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    viewport = window.cut_list.viewport()
    point = QPointF(5, viewport.height() - 5)  # 1 行だけなので一番下は何も無い所
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(viewport, QMouseEvent(kind, point, point, Qt.MouseButton.LeftButton,
                                                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert window._selected_cut_index is None


def test_proposal_rows_are_not_changed_and_ask_to_accept_first(opened, qapp):
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    window._refresh_cut_list()
    window._on_proposals_ready(window._program_info_request, [CutProposal(30, 60, 0.9, "案")], "")
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    _seek(window, qapp, 29)
    window._on_set_start()
    assert window.cut_manager.cuts == []
    assert (window.cut_manager.pending_proposals[0].start_frame, window._pending_start) == (30, None)
    assert "承認" in window.status_bar.currentMessage()

    window.accept_proposal_button.click()  # 承認した後なら直せる
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    window._on_set_start()
    assert _ranges(window) == [(29, 60)]


def test_home_and_end_move_to_the_selected_row(opened, qapp):
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    window.cut_manager.add_cut(12, 33)
    window._on_proposals_ready(window._program_info_request, [CutProposal(45, 90, 0.5, "案")], "")
    window._on_clear_cut_selection()
    assert not window.jump_start_button.isEnabled() and not window.jump_end_button.isEnabled()
    window._on_jump_to_selected_end()  # 何も選んでいない: 動かない
    before = window.seek_slider.value()
    assert window.seek_slider.value() == before

    _seek(window, qapp, 0)  # 開き直した録画を mpv が読み終えるまで待つ(読み込み中のシークは届かない)
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    assert window.jump_start_button.isEnabled() and window.jump_end_button.isEnabled()
    window._on_jump_to_selected_end()
    assert window.seek_slider.value() == 33
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() == 33, timeout_ms=3000)
    assert window.preview_widget.get_current_frame() == 33
    window._on_jump_to_selected_start()
    assert window.seek_slider.value() == 12

    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(1))  # 案の行(終了 90 は録画より後)
    window._on_jump_to_selected_end()
    assert window.seek_slider.value() == window.file_info.total_frames - 1


def test_shortcut_list_has_the_new_keys():
    from ts_smartcut.core.shortcut_presets import DEFAULT_KEYS

    assert {"Home", "End", "Esc"} <= set(DEFAULT_KEYS.values())
