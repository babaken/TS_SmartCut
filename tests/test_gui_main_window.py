import dataclasses

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

import pytest

from tests._qt_helpers import pump_events
from ts_smartcut.gui import main_window as main_window_module
from ts_smartcut.gui import preview_widget as preview_widget_module
from ts_smartcut.gui.main_window import MainWindow
from ts_smartcut.gui.theme import ThemeManager
from PySide6.QtGui import QKeySequence
from ts_smartcut.core.shortcut_presets import ShortcutPresetManager

# windowフィクスチャ(module scope)はtests/conftest.pyで定義されている。

# ---- 基本構造 (ファイル未読み込みの初期状態に依存するため先頭で実行) --------

def test_window_title(window):
    assert window.windowTitle() == "TS_SmartCut"


def test_menu_top_level_structure(window):
    labels = [action.text() for action in window.menuBar().actions()]
    assert labels == ["ファイル", "編集", "表示", "ツール", "ヘルプ"]


def test_help_menu_about_shows_version(window, monkeypatch):
    from ts_smartcut import __version__

    shown = []
    monkeypatch.setattr(QMessageBox, "about", lambda parent, title, text: shown.append((title, text)))
    help_menu = window.menuBar().actions()[-1].menu()
    about_action = next(a for a in help_menu.actions() if a.text() == "バージョン情報")
    assert about_action.isEnabled() is True

    about_action.trigger()

    assert len(shown) == 1
    assert shown[0][0] == "バージョン情報"
    assert f"TS_SmartCut {__version__}" in shown[0][1]


def test_theme_applied_by_default(window):
    assert window.theme == ThemeManager.get_theme("light")


def test_playback_controls_disabled_before_file_loaded(window):
    for widget in (
        window.back5s_button,
        window.frame_back_button,
        window.play_pause_button,
        window.frame_forward_button,
        window.forward5s_button,
        window.seek_slider,
        window.speed_combo,
    ):
        assert widget.isEnabled() is False


def test_mpv_connects_when_available(window):
    assert window.preview_widget.is_connected is True
    assert "接続" in window.status_bar.currentMessage()


def test_open_nonexistent_file_shows_error_without_crashing(window, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: errors.append(a))

    window.open_file(tmp_path / "does_not_exist.mp4")

    assert window.file_info is None
    assert len(errors) == 1


# ---- ファイルを開く・再生制御 (以降は実際にファイルを読み込んだ状態で進行) ------

def test_open_file_loads_info_and_enables_controls(window, sample_video):
    window.open_file(sample_video)

    assert window.file_info is not None
    assert window.file_info.total_frames == 75
    assert "sample.mp4" in window.windowTitle()
    assert "sample.mp4" in window.file_info_label.text()
    assert window.seek_slider.maximum() == 74
    assert window.play_pause_button.isEnabled() is True


def _forget_registrations(window):
    """他のテスト(バッチ処理)が同じサンプル動画を記録 DB に登録していることがあるので消す
    (登録済みなら、ファイル情報の行の先頭に「登録済み」が付く)。"""
    with window.history_db.get_connection() as conn:
        conn.execute("DELETE FROM recordings")


def test_file_info_line_shows_station_and_program(window, sample_video, qapp):
    # 番組情報(自動カット A1)は裏で読み、読めたらファイル情報の行の先頭に局名・番組名を出す
    from datetime import datetime, timedelta

    from ts_smartcut.broadcast.program_info import JST, EventInfo, ProgramInfo

    _forget_registrations(window)
    window.open_file(sample_video)
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert window.program_info is None  # MP4 には番組情報が無い
    assert window.file_info_label.text().startswith("sample.mp4")
    base = window.file_info_label.text()

    start = datetime(2026, 9, 30, 22, 0, tzinfo=JST)
    info = ProgramInfo(
        service_id=0x100, service_name="ＣＳ－Ａ",
        recording_start=start - timedelta(seconds=4), recording_end=start + timedelta(minutes=50, seconds=2),
        event=EventInfo(1, start, timedelta(minutes=50), name="番組名 #85", description="説明"),
    )
    window._on_program_info_ready(window._program_info_request - 1, info)  # 前に開いたファイルの結果
    assert window.file_info_label.text() == base
    window._on_program_info_ready(window._program_info_request, info)
    assert window.file_info_label.text() == "ＣＳ－Ａ  番組名 #85  |  " + base
    tooltip = window.file_info_label.toolTip()
    assert "放送: 2026/09/30 22:00〜22:50" in tooltip
    assert "録画: 2026/09/30 21:59:56〜22:50:02" in tooltip
    assert "説明" in tooltip
    assert tooltip.endswith(base)  # 行は幅で切れるので、全文はツールチップで見られる
    # 長い行でもウィンドウの幅を押し広げない
    window.file_info_label.setText("x" * 2000)
    assert window.minimumSizeHint().width() < 1200
    window._show_file_info()


def test_window_fits_a_1080p_screen_at_150_percent(window):
    """プレビューに最小の高さが無く、1080p の画面を 150% で使った時の作業領域
    (論理ピクセルで高さ約 690、タイトルバーを除くと約 660)に収まる。"""
    assert window.minimumSizeHint().height() < 660
    assert window.preview_stack.minimumSizeHint().height() < 100


def test_file_info_line_shows_a_registered_video(window, sample_video, qapp, monkeypatch):
    """自動カットの記録に登録済みの動画(先頭のハッシュとサイズが同じ)を開くと、ファイル情報の
    行の先頭に「登録済み」を出し、ツールチップに登録日時・最後のカットの日時・回数を出す。"""
    from datetime import datetime

    from ts_smartcut.autocut.recorder import AutoCutRecorder

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    _forget_registrations(window)
    window.open_file(sample_video)
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert window.registration is None
    base = window.file_info_label.text()
    assert not base.startswith("登録済み")

    window.cut_manager.add_cut(0, 25)
    window.file_info.cuts = list(window.cut_manager.cuts)
    recorder = AutoCutRecorder(window.history_db)
    assert recorder.record_batch_done(window.file_info, "smart_cut") is not None
    assert recorder.record_batch_done(window.file_info, "smart_cut") is not None

    window.open_file(sample_video)
    pump_events(qapp, lambda: window.registration is not None)
    assert window.registration["cut_count"] == 2
    assert window.file_info_label.text() == "登録済み  |  " + base
    tooltip = window.file_info_label.toolTip()
    today = f"{datetime.now():%Y/%m/%d}"
    assert f"自動カットの記録に登録済み: 登録 {today}" in tooltip and "（カット 2 回）" in tooltip
    assert tooltip.endswith(base)

    # 後のテストで「登録済み」が出ないよう、記録を消して開き直す
    _forget_registrations(window)
    window.open_file(sample_video)
    pump_events(qapp, lambda: False, timeout_ms=300)


def test_opening_a_file_does_not_start_playback_and_sound_starts_muted(window, sample_video, qapp):
    # ファイルを開いても自動で再生しない。音は初期状態で消えている(利用者の要件 2026-10-06)
    mpv = window.preview_widget.controller._mpv
    mpv.pause = False  # 前のファイルを再生中だった
    window.open_file(sample_video)
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() is not None)
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert mpv.pause is True
    assert window.preview_widget.get_current_frame() == 0
    assert mpv.mute is True
    assert window.mute_button.isChecked() and "消音" in window.mute_button.text()


def test_mute_button_and_m_key_toggle_sound(window):
    mpv = window.preview_widget.controller._mpv
    window.mute_button.setChecked(False)
    assert mpv.mute is False and "音あり" in window.mute_button.text()
    window._on_toggle_mute_key()
    assert mpv.mute is True and window.mute_button.isChecked()


def test_frame_steps_are_silent_and_playback_restores_sound(window, qapp):
    mpv = window.preview_widget.controller._mpv
    window.mute_button.setChecked(False)  # 音を出す設定にしても
    mpv.pause = True
    window._on_frame_forward()
    assert mpv.mute is True  # フレーム送りでは鳴らさない
    window._on_frame_back()
    assert mpv.mute is True
    window._on_toggle_pause()  # 再生を始めると音が戻る
    assert mpv.mute is False
    window._on_toggle_pause()
    window.mute_button.setChecked(True)


def test_frame_step_forward_advances_current_frame(window, qapp):
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() is not None)
    # 前のテストのフレーム送り・戻し・再生/一時停止はmpvの処理を待たずに終わる。
    # 戻しが終わる前の位置(1)を開始位置として読むと、戻し(0)→送り(1)で
    # 「進んでいない」に見えて落ちた(混んでいる時に10〜15回に1回、2026-10-09)。
    # シーク中でなく、位置が0.3秒動かなくなってから読む。
    mpv = window.preview_widget.controller._mpv
    recent = []

    def settled():
        recent.append(window.preview_widget.get_current_frame())
        del recent[:-15]  # 20ms間隔で0.3秒分
        return len(recent) == 15 and len(set(recent)) == 1 and not mpv.seeking

    assert pump_events(qapp, settled, timeout_ms=5000)
    start_frame = window.preview_widget.get_current_frame()

    window._on_frame_forward()
    pump_events(
        qapp,
        lambda: (window.preview_widget.get_current_frame() or 0) > start_frame,
    )

    assert window.preview_widget.get_current_frame() > start_frame


def test_seek_slider_release_seeks(window, qapp):
    window.seek_slider.setValue(30)
    window._on_seek_slider_released()
    pump_events(
        qapp,
        lambda: 28 <= (window.preview_widget.get_current_frame() or -1) <= 32,
    )

    frame = window.preview_widget.get_current_frame()
    assert 28 <= frame <= 32


def test_a_click_on_the_seek_bar_groove_seeks_there(window, qapp):
    """溝のクリックはその位置へシークする(Qt の既定は 1 ページ動くだけでシークしなかった)。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    slider = window.seek_slider
    target = slider.maximum() * 3 // 4
    x = next(x for x in range(slider.width()) if slider.value_at(x) >= target)
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(x, slider.height() // 2))
    assert abs(slider.value() - target) <= 1
    pump_events(qapp, lambda: abs((window.preview_widget.get_current_frame() or -1) - target) <= 2)
    assert abs(window.preview_widget.get_current_frame() - target) <= 2
    assert window._seek_slider_dragging is False


def test_speed_control_changes_mpv_speed(window):
    window.speed_combo.setCurrentText("2.0")
    assert window.preview_widget.controller._mpv.speed == pytest.approx(2.0)

    window.speed_combo.setCurrentText("1.0")
    assert window.preview_widget.controller._mpv.speed == pytest.approx(1.0)


def test_toggle_pause_changes_playback_state(window, qapp):
    initial_pause = window.preview_widget.controller._mpv.pause

    window._on_toggle_pause()
    pump_events(qapp, lambda: window.preview_widget.controller._mpv.pause != initial_pause, timeout_ms=1000)

    assert window.preview_widget.controller._mpv.pause != initial_pause


def test_position_polling_updates_time_label(window, qapp):
    window.preview_widget.controller._mpv.pause = False
    pump_events(qapp, lambda: window.time_label.text() != "00:00:00.000 (Frame: 0 / 75)", timeout_ms=2000)

    assert window.time_label.text() != "00:00:00.000 (Frame: 0 / 75)"


# ---- テーマ切替 -----------------------------------------------------------

def test_set_theme_updates_settings_and_style(window):
    window._set_theme("dark")
    assert window.settings.theme == "dark"
    assert window.theme == ThemeManager.get_theme("dark")
    window._set_theme("light")


# ---- mpv未導入時のフォールバック (実mpvインスタンスは作らないため独立実行可) ---

def test_mpv_unavailable_falls_back_to_placeholder(qapp, monkeypatch):
    monkeypatch.setattr(preview_widget_module, "create_controller", lambda *a, **k: None)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

    w = MainWindow()
    w.show()
    pump_events(qapp, lambda: w.preview_widget.controller is None or w.preview_widget.is_connected, timeout_ms=3000)
    # initializeGL()は一度しか呼ばれないため、上のpumpは
    # 「connection_readyが発火するまで待つ」という程度の意味で使う。
    pump_events(qapp, lambda: len(warnings) > 0, timeout_ms=3000)

    try:
        assert w.preview_widget.is_connected is False
        assert w.preview_stack.currentWidget() is w.preview_placeholder
        assert "利用不可" in w.status_bar.currentMessage()
        assert len(warnings) == 1
    finally:
        w.close()
        w.deleteLater()
        qapp.processEvents()
        qapp.processEvents()


# ---- 設定 (Phase 9: 永続化・ログ再設定) -------------------------------------

def test_open_settings_dialog_saves_and_reconfigures_logging(window, tmp_path, monkeypatch):
    from ts_smartcut.core import settings_manager as settings_manager_module
    from ts_smartcut.gui import main_window as mw_module

    settings_path = tmp_path / "settings.json"
    log_path = tmp_path / "logs" / "app.log"
    monkeypatch.setattr(settings_manager_module, "get_default_settings_path", lambda: settings_path)
    monkeypatch.setattr(mw_module, "configure_logging", lambda settings: mw_module.logging.getLogger("ts_smartcut"))

    def _fake_exec(self):
        self.threads_spin.setValue(6)
        self.show_waveform_check.setChecked(False)
        return True

    monkeypatch.setattr(mw_module.SettingsDialog, "exec", _fake_exec)

    window._on_open_settings()

    assert window.settings.cpu_threads == 6
    saved = settings_manager_module.SettingsManager.load(settings_path)
    assert saved.cpu_threads == 6
    # 音声波形の表示/非表示も設定で切り替わり、保存される
    assert saved.show_waveform is False
    assert not window.waveform_view.isVisible() and not window.waveform_action.isChecked()
    window.waveform_action.setChecked(True)
    # バッチ処理側も同じ設定オブジェクトを見ていること（再起動不要で反映される）
    assert window.batch_manager.settings.cpu_threads == 6
    assert window.batch_manager.output_manager.settings is window.settings


def test_theme_changed_in_the_settings_dialog_is_applied_at_once(window, tmp_path, monkeypatch):
    """設定画面で変えたテーマは閉じた時にすぐ反映する(以前は配色を読み直すだけだった)。
    「OS に従う」も選べる(2026-10-09)。"""
    from ts_smartcut.core import settings_manager as settings_manager_module
    from ts_smartcut.gui import main_window as mw_module
    from ts_smartcut.gui.theme import SYSTEM_THEME, THEMES, resolve_theme_name

    monkeypatch.setattr(settings_manager_module, "get_default_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(mw_module, "configure_logging", lambda settings: mw_module.logging.getLogger("ts_smartcut"))
    chosen = []

    def _fake_exec(self):
        assert self.theme_combo.findText(SYSTEM_THEME) >= 0
        self.theme_combo.setCurrentText(chosen[-1])
        return True

    monkeypatch.setattr(mw_module.SettingsDialog, "exec", _fake_exec)
    try:
        for name in ("dark", SYSTEM_THEME):
            chosen.append(name)
            window._on_open_settings()
            assert window.settings.theme == name
            assert window.theme == THEMES[resolve_theme_name(name)]
            assert window.timeline._theme == window.theme
    finally:
        window._set_theme("light")


def test_system_theme_follows_os_changes(window, monkeypatch):
    """「OS に従う」の時だけ、OS のライト/ダークの切り替えで配色を変える。"""
    from ts_smartcut.gui.theme import SYSTEM_THEME

    calls = []
    monkeypatch.setattr(window, "_set_theme", calls.append)
    monkeypatch.setattr(window.settings, "theme", SYSTEM_THEME)
    window._on_os_color_scheme_changed()
    window.settings.theme = "light"
    window._on_os_color_scheme_changed()  # 固定のテーマの時は何もしない
    assert calls == [SYSTEM_THEME]


def test_add_to_batch_uses_default_processing_mode_setting(window, sample_video, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.open_file(sample_video)
    window.cut_manager.add_cut(0, 25)
    window.settings.default_processing_mode = "full_reencode"
    try:
        window._on_add_to_batch()
        assert window.batch_manager.items[-1].processing_mode == "full_reencode"
        assert window.batch_list_view.tree.topLevelItem(
            window.batch_list_view.tree.topLevelItemCount() - 1
        ).text(4) == "Full Re-encode"
    finally:
        window.settings.default_processing_mode = "smart_cut"


def test_add_to_batch_without_keep_ranges_is_refused(window, sample_video, monkeypatch):
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: messages.append(text))
    window.open_file(sample_video)
    window.batch_manager.items.clear()
    window._on_add_to_batch()
    assert window.batch_manager.items == []
    assert messages and "残す区間がありません" in messages[0]


def _open_as_vfr(window, sample_video, monkeypatch):
    real_get_file_info = window.ffmpeg.get_file_info
    monkeypatch.setattr(window.ffmpeg, "get_file_info",
                        lambda path: dataclasses.replace(real_get_file_info(path), is_vfr=True))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.open_file(sample_video)
    return warnings


def test_opening_vfr_file_warns_and_batch_shows_simple_copy(window, sample_video, monkeypatch):
    # design doc §16.3: VFRではSmart CutがSimple Copyになることを開いた時に知らせる
    warnings = _open_as_vfr(window, sample_video, monkeypatch)
    try:
        assert len(warnings) == 1 and "可変フレームレート" in warnings[0]
        window.batch_manager.items.clear()  # 前のテストで同じファイルを追加済み
        window.cut_manager.add_cut(0, 25)
        window._on_add_to_batch()
        last = window.batch_list_view.tree.topLevelItemCount() - 1
        assert window.batch_list_view.tree.topLevelItem(last).text(4) == "Simple Copy（VFR）"
    finally:
        window.batch_manager.items.clear()
        window.batch_list_view._refresh_list()


def test_opening_vfr_file_does_not_warn_without_smart_cut(window, sample_video, monkeypatch):
    window.settings.default_processing_mode = "simple_copy"
    try:
        assert _open_as_vfr(window, sample_video, monkeypatch) == []
    finally:
        window.settings.default_processing_mode = "smart_cut"


def test_constant_frame_rate_file_does_not_warn(window, sample_video, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    window.open_file(sample_video)
    assert window.file_info.is_vfr is False and warnings == []


def test_file_info_line_shows_scan_type_and_bitrate(window, sample_video):
    # 再エンコードは元の走査方式・ビットレートに合わせるので、開いた時に見えるようにする
    window.open_file(sample_video)
    text = window.file_info_label.text()
    assert "プログレッシブ" in text and "Mbps" in text


def test_status_bar_shows_free_space_of_output_dir(window, sample_video, tmp_path):
    import shutil

    window.open_file(sample_video)
    expected = shutil.disk_usage(sample_video.parent).free / 1e9
    text = window.disk_free_label.text()
    assert text.startswith("出力先の空き: ") and abs(float(text.split()[1]) - expected) < 1.0
    assert window.disk_free_label.toolTip() == str(sample_video.parent)

    # 設定の出力先(まだ無いフォルダでも)が優先
    window.settings.default_output_dir = tmp_path / "not" / "yet"
    try:
        window._update_disk_free()
        assert window.disk_free_label.toolTip() == str(tmp_path / "not" / "yet")
        assert window.disk_free_label.text().startswith("出力先の空き: ")
        assert "不明" not in window.disk_free_label.text()
    finally:
        window.settings.default_output_dir = None


def test_cut_list_title_shows_total_cut_time(window, sample_video, monkeypatch):
    # design doc §4.8: 「カット区間 (HH:MM:SS.mmm)」。sample_videoは25fps
    window.open_file(sample_video)
    assert window.cut_list_title.text() == "残す区間（確定再生時間：00:00:00.000　計算再生時間：―）"

    window.cut_manager.add_cut(0, 25)    # 1秒
    window.cut_manager.add_cut(50, 63)   # 0.52秒
    window._refresh_cut_list()
    assert window.cut_list_title.text() == "残す区間（確定再生時間：00:00:01.520　計算再生時間：―）"

    window._on_undo()  # Undo/Redo・すべてクリアでも更新される
    assert window.cut_list_title.text() == "残す区間（確定再生時間：00:00:01.000　計算再生時間：―）"
    window._on_redo()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._on_clear_all()
    assert window.cut_list_title.text() == "残す区間（確定再生時間：00:00:00.000　計算再生時間：―）"


def test_cut_list_title_shows_the_computed_time_of_the_proposals(window, sample_video):
    # 計算再生時間 = 自動カットが出した案すべての合計。承認・棄却しても変わらない(2026-10-11)。
    # sample_videoは25fps
    from ts_smartcut.models import CutProposal

    window.open_file(sample_video)
    first, second = CutProposal(0, 25, 0.9, "テスト"), CutProposal(50, 100, 0.4, "テスト")
    window.cut_manager.set_proposals([first, second])
    window._refresh_cut_list()
    assert window.cut_list_title.text() == "残す区間（確定再生時間：00:00:00.000　計算再生時間：00:00:03.000）"

    window.cut_manager.accept_proposal(first)
    window.cut_manager.reject_proposal(second)
    window._refresh_cut_list()
    assert window.cut_list_title.text() == "残す区間（確定再生時間：00:00:01.000　計算再生時間：00:00:03.000）"


def test_clear_all_asks_then_clears_and_undo_restores(window, sample_video, monkeypatch):
    window.open_file(sample_video)
    window.cut_manager.add_cut(5, 10)
    window.bookmark_manager.add_bookmark(20, "CM")
    window._refresh_cut_list()
    window._refresh_bookmark_list()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    window._on_clear_all()
    assert window.cut_list.topLevelItemCount() == 1  # 確認で「いいえ」なら消さない

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._on_clear_all()
    assert window.cut_list.topLevelItemCount() == 0 and window.bookmark_list.topLevelItemCount() == 0

    window._on_undo()
    assert window.cut_list.topLevelItemCount() == 1 and window.bookmark_list.topLevelItemCount() == 1


# ---- ファイルのドロップ (design doc §4.6) ----------------------------------------

def _drop_event(paths):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    return event, mime  # mimeは event より先に破棄されないよう呼び出し側で保持する


@pytest.fixture
def sample_ts(sample_video, tmp_path):
    import subprocess

    path = tmp_path / "録画.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(sample_video), "-c", "copy", str(path)], check=True)
    return path


def test_dropping_a_ts_file_opens_it(window, sample_ts, qapp):
    event, _mime = _drop_event([sample_ts])
    window.dragEnterEvent(event)
    assert event.isAccepted()
    window.dropEvent(event)
    qapp.processEvents()  # 開くのはドロップ処理の後
    assert window.file_info.file_path == sample_ts
    assert window.recent_files.files[0] == sample_ts


@pytest.mark.parametrize(
    "names", [["video.mp4"], ["a.ts", "b.ts"], ["notes.txt"], ["録画.ts.keyframes.json"]]
)
def test_unsupported_drops_are_rejected(window, tmp_path, names):
    paths = [tmp_path / n for n in names]
    for p in paths:
        p.write_bytes(b"")
    event, _mime = _drop_event(paths)
    window.dragEnterEvent(event)
    assert not event.isAccepted()


def test_add_to_batch_takes_a_snapshot_of_the_keep_ranges(window, sample_video, monkeypatch):
    """追加の後の「基準外」の切り替え・区間の編集は、バッチの項目に影響しない(エンコード開始と同じ)。"""
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.open_file(sample_video)
    window.batch_manager.items.clear()
    window.cut_manager.add_cut(0, 25)
    try:
        window._on_add_to_batch()
        window.cut_manager.toggle_excluded(0)
        window.cut_manager.edit_cut(0, 5, 25)
        item = window.batch_manager.items[-1]
        assert item.file_info is not window.file_info
        assert [(c.start_frame, c.end_frame, c.is_excluded) for c in item.file_info.cuts] == [(0, 25, False)]
        assert [e["event_type"] for e in item.edit_events] == ["add_cut"]
    finally:
        window.batch_manager.items.clear()


def test_close_is_refused_while_features_are_computed(window, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: messages.append(text))
    window.feature_worker._idle.clear()  # 計算中にする
    try:
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert messages and "特徴を計算中" in messages[0]
    finally:
        window.feature_worker._idle.set()


def test_history_db_error_is_shown_and_recording_is_disabled(window, qapp, monkeypatch):
    from ts_smartcut.errors import ProjectError

    def newer_db():
        raise ProjectError("E403", "記録 DB の版 (v9) がこのアプリの対応する版 (v3) より新しいため使えません")

    errors = []
    monkeypatch.setattr(main_window_module, "CutHistoryDB", newer_db)
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))
    assert window._open_history_db() is None
    pump_events(qapp, lambda: bool(errors))
    assert errors and "[E403]" in errors[0] and "記録" in errors[0]


# ---- 終了 (Phase 6/7で実機発見したXエラー等の回帰防止。最後に実行する) --------

def test_close_after_playback_does_not_raise(window):
    window.preview_widget.controller._mpv.pause = False
    window.close()  # 例外が出ないことを確認する


def test_lists_are_tabs_and_the_view_menu_shows_or_hides_them(window):
    # 残す区間・ブックマーク・バッチリストはタブ(前は2つが横並びでバッチリストは別の窓)。
    # 表示メニューで出す・隠すと設定に入る(起動時に戻す)。全部隠すとタブの場所ごと消える
    tabs = window.lists_tabs
    pages = [window.cut_list_page, window.bookmark_list, window.batch_list_view]
    assert [tabs.indexOf(p) for p in pages] == [0, 1, 2]
    actions = [window.cut_list_tab_action, window.bookmark_tab_action, window.batch_tab_action]
    keys = ["show_cut_list_tab", "show_bookmark_tab", "show_batch_tab"]
    try:
        window.bookmark_tab_action.setChecked(False)
        assert window.settings.show_bookmark_tab is False
        assert not tabs.isTabVisible(1) and tabs.isTabVisible(0) and tabs.isTabVisible(2)
        for action in actions:
            action.setChecked(False)
        assert not tabs.isVisibleTo(window)
        # ツール→バッチリストを開く: 隠していれば出してそのタブにする
        window._on_show_batch_tab()
        assert window.settings.show_batch_tab is True
        assert tabs.isVisibleTo(window) and tabs.currentWidget() is window.batch_list_view
    finally:
        for action in actions:
            action.setChecked(True)
        tabs.setCurrentWidget(window.cut_list_page)
    assert all(getattr(window.settings, k) for k in keys)


def test_tab_visibility_and_batch_column_widths_follow_the_settings(window):
    # 起動時は設定どおり(_build_layout と同じ処理)。列の幅をドラッグすると設定に入る
    # (mpv を2つ作らないよう、同じ窓で確かめる)
    saved = list(window.settings.batch_column_widths)
    try:
        window.settings.show_bookmark_tab = False
        window._apply_tab_visibility()
        assert not window.lists_tabs.isTabVisible(window.lists_tabs.indexOf(window.bookmark_list))
        window.batch_list_view.set_column_widths([70, 345, 250, 85, 140, 90, 200])
        assert window.batch_list_view.column_widths()[:6] == [70, 345, 250, 85, 140, 90]
        window.batch_list_view.tree.header().resizeSection(1, 300)
        assert window.settings.batch_column_widths[1] == 300
    finally:
        window.settings.show_bookmark_tab = True
        window._apply_tab_visibility()
        window.settings.batch_column_widths = saved


def test_double_click_on_a_keep_range_opens_the_edit(window, sample_video, monkeypatch):
    # 残す区間(手で入れた・承認した案)の行をダブルクリックすると区間編集。未判断の案の行は何もしない
    from PySide6.QtWidgets import QInputDialog

    from ts_smartcut.models import CutProposal

    window.open_file(sample_video)
    window.cut_manager.clear_all()  # 前のテストの区間が残っていることがある
    window.cut_manager.add_cut(0, 25)
    window.cut_manager.set_proposals([CutProposal(50, 75, 0.9, "テスト")])
    window._refresh_cut_list()
    answers = iter([5, 20])
    asked = []

    def get_int(*args, **kwargs):
        asked.append(args[2])
        return next(answers), True

    monkeypatch.setattr(QInputDialog, "getInt", get_int)
    window.cut_list.itemDoubleClicked.emit(window.cut_list.topLevelItem(1), 0)  # 案の行
    assert asked == []
    window.cut_list.itemDoubleClicked.emit(window.cut_list.topLevelItem(0), 0)
    assert asked == ["開始フレーム:", "終了フレーム:"]
    assert [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts] == [(5, 20)]


def test_add_to_batch_asks_the_output_name_and_allows_episodes(window, sample_video, monkeypatch):
    # バッチ追加のたびに保存名称を尋ねる。1本の録画の2つの話を名前を変えて追加できる。
    # キャンセルなら追加しない(2026-10-11)
    from ts_smartcut.gui import output_name_dialog

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    # 前のテストの未判断の案が残っていても確認で止まらない
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.batch_manager.items.clear()
    window.open_file(sample_video)
    window.cut_manager.clear_all()
    window.cut_manager.add_cut(0, 10)
    answers = iter([None, "第1話", "第2話"])
    initials = []

    def answer(parent, initial, input_path, check, title="保存名称"):
        initials.append(initial)
        name = next(answers)
        return None if name is None else initial.with_name(name + initial.suffix)

    monkeypatch.setattr(output_name_dialog, "ask_output_name", answer)
    try:
        window._on_add_to_batch()
        assert window.batch_manager.items == []  # キャンセル
        window._on_add_to_batch()
        window.cut_manager.clear_all()
        window.cut_manager.add_cut(15, 25)
        window._on_add_to_batch()
        items = window.batch_manager.items
        assert [item.output_path.name for item in items] == ["第1話" + sample_video.suffix, "第2話" + sample_video.suffix]
        assert [(c.start_frame, c.end_frame) for c in items[1].file_info.cuts] == [(15, 25)]
        # 2つ目の初期値は1つ目(バッチの他の項目)と重ならない名前
        assert initials[2] != items[0].output_path
    finally:
        window.batch_manager.items.clear()
        window.batch_list_view._refresh_list()


# ---- キープリセット (設計書 §4.5.1) ----------------------------------------------
# window(mpv)をもう 1 つ作らないようにここに置く(test_shortcut_presets.py に置くと、全部のテストを
# 流した時にその窓を作るところで落ちた)


@pytest.fixture
def window_with_presets(window, tmp_path):
    """window(モジュールで 1 つ)のキープリセットを tmp のものに差し替え、終わったら戻す。"""
    saved = window.shortcut_presets
    window.shortcut_presets = ShortcutPresetManager(tmp_path / "shortcut_presets.json")
    window._apply_shortcut_keys()
    yield window
    window.shortcut_presets = saved
    window._apply_shortcut_keys()


def _key(window, action):
    return window._shortcut_by_action[action].key().toString(QKeySequence.SequenceFormat.PortableText)


def test_the_window_uses_the_active_preset(window_with_presets):
    window = window_with_presets
    assert _key(window, "set_start") == "S"
    assert _key(window, "accept_proposal") == ""
    assert window.set_start_button.text().endswith("(S)")

    preset = window.shortcut_presets.presets[1]
    preset.set_key("set_start", "H")
    preset.set_key("accept_proposal", "R")
    window._on_choose_shortcut_preset(1)
    assert _key(window, "set_start") == "H"
    assert _key(window, "accept_proposal") == "R"
    assert window.set_start_button.text().endswith("(H)")
    assert window.accept_proposal_button.toolTip().endswith("(R)")
    assert window.shortcut_presets.active_index == 1


def test_an_unassigned_action_shows_no_key(window_with_presets):
    window = window_with_presets
    window.shortcut_presets.active.set_key("set_end", "")
    window._apply_shortcut_keys()
    assert "(" not in window.set_end_button.text()
    assert window._shortcut_by_action["set_end"].key().isEmpty()


def test_the_view_menu_lists_five_presets_with_names(window_with_presets):
    window = window_with_presets
    window.shortcut_presets.presets[3].name = "左手デバイス"
    window._rebuild_shortcut_preset_menu()
    actions = window.shortcut_preset_menu.actions()
    assert len(actions) == 5
    assert actions[3].text() == "左手デバイス"
    assert actions[0].isChecked()
    actions[3].trigger()
    assert window.shortcut_presets.active_index == 3
    assert [a.isChecked() for a in window.shortcut_preset_menu.actions()] == [False, False, False, True, False]
