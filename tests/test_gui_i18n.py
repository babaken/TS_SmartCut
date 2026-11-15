"""言語切替のGUI統合テスト (design doc §13.4: 再起動後に反映)。

設定変更自体はライブ反映ではなく次回起動時に反映される仕様のため、
「settings.languageを変えて新しいMainWindowを作る」ことが実際の反映
経路になる。"""

import pytest

from tests._qt_helpers import pump_events
from ts_smartcut.gui.main_window import MainWindow
from ts_smartcut.models import AppSettings


def _make_window(qapp, language: str) -> MainWindow:
    w = MainWindow(AppSettings(language=language))
    w.show()
    pump_events(qapp, lambda: w.preview_widget.is_connected, timeout_ms=3000)
    return w


def _cleanup(qapp, window: MainWindow) -> None:
    window.close()
    window.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def test_default_jp_menu_labels(qapp):
    window = _make_window(qapp, "jp")
    try:
        labels = [a.text() for a in window.menuBar().actions()]
        assert labels == ["ファイル", "編集", "表示", "ツール", "ヘルプ"]
        assert window.set_start_button.text() == "Set Start (S)"
        assert window.file_info_label.text() == "ファイル未読み込み"
    finally:
        _cleanup(qapp, window)


def test_english_menu_labels(qapp):
    window = _make_window(qapp, "en")
    try:
        labels = [a.text() for a in window.menuBar().actions()]
        assert labels == ["File", "Edit", "View", "Tools", "Help"]
        assert window.file_info_label.text() == "No file loaded"
        assert window.cut_list.headerItem().text(0) == "Start"
        assert window.bookmark_list.headerItem().text(1) == "Name"
        # 設計書 §13: メイン画面の部品
        assert window.accept_all_proposals_button.text() == "Accept all"
        assert window.mute_button.text() == "🔇 Muted"
    finally:
        _cleanup(qapp, window)


def test_unsupported_language_falls_back_to_jp(qapp):
    window = _make_window(qapp, "fr")
    try:
        assert window.lang.lang_code == "jp"
        labels = [a.text() for a in window.menuBar().actions()]
        assert labels == ["ファイル", "編集", "表示", "ツール", "ヘルプ"]
    finally:
        _cleanup(qapp, window)


def test_language_change_via_settings_dialog_is_saved_but_not_live(qapp, tmp_path, monkeypatch):
    """design doc §13.4: 言語切替は再起動後に反映される。設定変更直後の
    実行中ウィンドウのメニュー文言はまだ切り替わらないことを確認する。"""
    from ts_smartcut.core import settings_manager as settings_manager_module

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_manager_module, "get_default_settings_path", lambda: settings_path)

    window = _make_window(qapp, "jp")
    try:
        from ts_smartcut.gui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(window.settings, window)
        assert [dialog.language_combo.itemText(i) for i in range(dialog.language_combo.count())] == ["日本語", "English"]
        dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("en"))
        window.settings = dialog.get_settings()
        settings_manager_module.SettingsManager.save(window.settings)

        # 実行中のウィンドウのメニューはまだ日本語のまま(再起動後に反映)。
        labels = [a.text() for a in window.menuBar().actions()]
        assert labels == ["ファイル", "編集", "表示", "ツール", "ヘルプ"]

        saved = settings_manager_module.SettingsManager.load(settings_path)
        assert saved.language == "en"
    finally:
        _cleanup(qapp, window)
