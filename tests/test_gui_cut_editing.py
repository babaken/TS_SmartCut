from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QInputDialog, QMessageBox

import pytest

from tests._qt_helpers import pump_events
from ts_smartcut.errors import TSSmartCutError
from ts_smartcut.gui import main_window as main_window_module

# windowフィクスチャ(module scope)はtests/conftest.pyで定義されている。
# このファイル専用の別インスタンス(別プロセスのmpv接続ではなく、同一
# プロセス内の別モジュールスコープ)を使う。


@pytest.fixture(autouse=True)
def opened(window, interlaced_sample_video, qapp):
    """テストごとに動画を開き直す(区間・ブックマーク・Start/End も空になる)。前は最初のテストが開き、
    後のテストがその状態と前のテストの区間・ブックマークを前提にしていたので、単独で回すと E104 で
    落ちた(2026-10-27)。開き直した直後は前の動画の位置が残っていて、読み込み中のシークは失敗するので、
    0 フレームへのシークが通って着くまで待つ。"""
    window.open_file(interlaced_sample_video)

    def seeked() -> bool:
        try:
            window.preview_widget.seek_frame(0)
        except TSSmartCutError:
            return False
        return True

    assert pump_events(qapp, seeked, timeout_ms=10000)
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() == 0, timeout_ms=3000)
    return window


def _seek_and_settle(window, qapp, frame, paused=True):
    """決定論的なテストのため再生を一時停止した状態でシークし、反映を待つ。"""
    if paused:
        window.preview_widget.controller._mpv.pause = True

    # 動画読込直後はfps未確定でMPVIPCErrorが出うる(design doc §2)。
    # 準備が整うまで待ってからシークする。
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() is not None, timeout_ms=3000)

    window.preview_widget.seek_frame(frame)
    pump_events(
        qapp,
        lambda: (window.preview_widget.get_current_frame() or -1) == frame,
        timeout_ms=2000,
    )
    # 精密シーク直後にframe-step/frame-back-stepを即座に発行すると、mpv内部
    # の状態遷移が完了しきっておらずコマンドが反映されないことが実機で
    # 確認された。位置確認後にもう一段階イベントループを回して安定させる。
    pump_events(qapp, lambda: False, timeout_ms=150)


def test_open_file_creates_cut_manager_and_keyframes(window, interlaced_sample_video):
    window.open_file(interlaced_sample_video)

    assert window.cut_manager is not None
    assert window.cut_manager.cuts == []
    assert window._keyframes == [0, 10, 20, 30, 40, 50, 60, 70]
    assert window.timeline._total_frames == 75


def test_open_file_builds_waveform_in_background_and_setting_toggles_display(window, qapp):
    # 音声波形は裏で作り、できたらプレビューの下に表示する
    pump_events(qapp, lambda: window._envelope is not None, timeout_ms=10000)
    assert len(window._envelope.peaks) > 0
    assert window.waveform_view._envelope is window._envelope
    assert window.waveform_view.isVisible()

    window.waveform_action.setChecked(False)
    assert not window.waveform_view.isVisible()
    assert window.settings.show_waveform is False

    window.waveform_action.setChecked(True)
    assert window.waveform_view.isVisible()
    assert window.settings.show_waveform is True


def test_set_start_and_set_end_update_pending_label(window, qapp):
    _seek_and_settle(window, qapp, 10)
    window._on_set_start()
    assert window._pending_start == 10
    assert "Start: 10" in window.pending_label.text()

    _seek_and_settle(window, qapp, 40)
    window._on_set_end()
    assert window._pending_end == 40
    assert "End: 40" in window.pending_label.text()


def test_add_cut_without_start_or_end_shows_warning(window, monkeypatch):
    window._pending_start = None
    window._pending_end = None
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

    window._on_add_cut()

    assert len(warnings) == 1
    assert window.cut_manager.cuts == []


def _add_cut(window, qapp, start=10, end=40):
    _seek_and_settle(window, qapp, start)
    window._on_set_start()
    _seek_and_settle(window, qapp, end)
    window._on_set_end()
    window._on_add_cut()


def test_add_cut_creates_range_and_updates_list_and_timeline(window, qapp):
    _add_cut(window, qapp)

    assert len(window.cut_manager.cuts) == 1
    cut = window.cut_manager.cuts[0]
    assert (cut.start_frame, cut.end_frame) == (10, 40)
    assert window.cut_list.topLevelItemCount() == 1
    assert window.cut_list.topLevelItem(0).text(0) == "10"
    assert window.cut_list.topLevelItem(0).text(1) == "40"
    assert window.timeline._cuts == window.cut_manager.cuts
    # Start/Endはリセットされる
    assert window._pending_start is None
    assert window._pending_end is None


def test_cut_selection_updates_timeline_highlight(window, qapp):
    _add_cut(window, qapp)
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    assert window._selected_cut_index == 0
    assert window.timeline._selected_cut_index == 0


def test_undo_redo_cut_operations(window, qapp):
    _add_cut(window, qapp)
    assert len(window.cut_manager.cuts) == 1

    window._on_undo()
    assert window.cut_manager.cuts == []
    assert window.cut_list.topLevelItemCount() == 0

    window._on_redo()
    assert len(window.cut_manager.cuts) == 1
    assert window.cut_list.topLevelItemCount() == 1


def test_edit_cut_updates_range(window, qapp, monkeypatch):
    _add_cut(window, qapp)
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    values = iter([(15, True), (45, True)])
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: next(values))

    window._on_edit_cut()

    cut = window.cut_manager.cuts[0]
    assert (cut.start_frame, cut.end_frame) == (15, 45)
    assert window.cut_list.topLevelItem(0).text(0) == "15"


def test_delete_cut_removes_from_manager_and_list(window, qapp):
    _add_cut(window, qapp)
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    window._on_delete_cut()

    assert window.cut_manager.cuts == []
    assert window.cut_list.topLevelItemCount() == 0
    assert window._selected_cut_index is None


# ---- ブックマーク ---------------------------------------------------------

def test_add_bookmark_updates_manager_list_and_timeline(window, qapp):
    _seek_and_settle(window, qapp, 25)
    window._on_add_bookmark()

    assert len(window.bookmark_manager.bookmarks) == 1
    assert window.bookmark_manager.bookmarks[0].frame == 25
    assert window.bookmark_list.topLevelItemCount() == 1
    assert window.timeline._bookmarks == window.bookmark_manager.bookmarks


def test_bookmark_double_click_seeks(window, qapp):
    _seek_and_settle(window, qapp, 25)
    window._on_add_bookmark()
    _seek_and_settle(window, qapp, 0)
    item = window.bookmark_list.topLevelItem(0)

    window._on_bookmark_double_clicked(item, 0)
    pump_events(qapp, lambda: (window.preview_widget.get_current_frame() or -1) == 25, timeout_ms=2000)

    assert window.preview_widget.get_current_frame() == 25


def test_remove_bookmark(window, qapp):
    _seek_and_settle(window, qapp, 25)
    window._on_add_bookmark()
    window.bookmark_list.setCurrentItem(window.bookmark_list.topLevelItem(0))
    window._on_remove_bookmark()

    assert window.bookmark_manager.bookmarks == []
    assert window.bookmark_list.topLevelItemCount() == 0


# ---- キーフレーム移動・速度ステップ -----------------------------------------

def test_prev_next_keyframe_navigation(window, qapp):
    _seek_and_settle(window, qapp, 35)

    window._on_prev_keyframe()
    pump_events(qapp, lambda: (window.preview_widget.get_current_frame() or -1) == 30, timeout_ms=2000)
    assert window.preview_widget.get_current_frame() == 30

    window._on_next_keyframe()
    pump_events(qapp, lambda: (window.preview_widget.get_current_frame() or -1) == 40, timeout_ms=2000)
    assert window.preview_widget.get_current_frame() == 40


def test_speed_step_cycles_and_clamps(window):
    window.speed_combo.setCurrentText("1.0")
    window._on_speed_step(1)
    assert window.speed_combo.currentText() == "1.5"
    window._on_speed_step(1)
    assert window.speed_combo.currentText() == "2.0"
    window._on_speed_step(1)  # 上限を超えても2.0のまま
    assert window.speed_combo.currentText() == "2.0"

    window.speed_combo.setCurrentText("0.25")
    window._on_speed_step(-1)  # 下限を下回っても0.25のまま
    assert window.speed_combo.currentText() == "0.25"

    window.speed_combo.setCurrentText("1.0")  # 他テストへの影響を避けて戻す


def test_timeline_click_seeks(window, qapp):
    _seek_and_settle(window, qapp, 0)
    window._on_timeline_clicked(20)
    pump_events(qapp, lambda: (window.preview_widget.get_current_frame() or -1) == 20, timeout_ms=2000)
    assert window.preview_widget.get_current_frame() == 20


# ---- マウスホイール操作 (design doc §4.5) -----------------------------------

def _wheel_event(y_delta: int, ctrl: bool = False) -> QWheelEvent:
    modifiers = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    return QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, y_delta),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False,
    )


def test_wheel_up_steps_back_one_frame(window, qapp):
    # 22フレーム目(キーフレームちょうどではない位置)から検証する。
    # frame-back-stepはキーフレーム位置からの巻き戻しだと直前のGOPまで
    # 遡って再デコードする必要があり、通常より時間がかかることが実機で
    # 確認された(タイムアウト値も余裕を持たせる)。
    _seek_and_settle(window, qapp, 22)

    window.wheelEvent(_wheel_event(120))
    pump_events(qapp, lambda: (window.preview_widget.get_current_frame() or -1) == 21, timeout_ms=4000)

    assert window.preview_widget.get_current_frame() == 21


def test_wheel_down_steps_forward_one_frame(window, qapp):
    _seek_and_settle(window, qapp, 22)

    window.wheelEvent(_wheel_event(-120))
    pump_events(qapp, lambda: (window.preview_widget.get_current_frame() or -1) == 23, timeout_ms=3000)

    assert window.preview_widget.get_current_frame() == 23


def test_ctrl_wheel_adjusts_volume(window):
    window.settings.playback_volume = 50

    window.wheelEvent(_wheel_event(120, ctrl=True))
    assert window.settings.playback_volume == 55
    assert window.preview_widget.controller._mpv.volume == pytest.approx(55)

    window.wheelEvent(_wheel_event(-120, ctrl=True))
    assert window.settings.playback_volume == 50


def test_ctrl_wheel_volume_clamped_to_valid_range(window):
    window.settings.playback_volume = 100
    window.wheelEvent(_wheel_event(120, ctrl=True))
    assert window.settings.playback_volume == 100

    window.settings.playback_volume = 0
    window.wheelEvent(_wheel_event(-120, ctrl=True))
    assert window.settings.playback_volume == 0
    window.settings.playback_volume = 100  # 他テストへの影響を避けて戻す
    window.preview_widget.set_volume(100)
