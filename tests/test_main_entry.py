"""ts_smartcut.main のエントリーポイント統合テスト。

実ユーザーの設定/ログディレクトリを汚さないよう、設定ファイル・
ログファイルのパスは必ずtmp_path配下へ差し替える。QApplication.exec()は
実イベントループをブロックするため、window.close()を予約するタイマー
経由でテストを終わらせる。
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from ts_smartcut import logging_setup as logging_setup_module
from ts_smartcut import main as main_module
from ts_smartcut.core import settings_manager as settings_manager_module


def test_main_loads_settings_configures_logging_and_saves_on_exit(qapp, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    log_path = tmp_path / "logs" / "app.log"

    monkeypatch.setattr(settings_manager_module, "get_default_settings_path", lambda: settings_path)
    monkeypatch.setattr(logging_setup_module, "get_default_log_path", lambda: log_path)

    # 設定ファイルを事前に用意し、ロード→反映→保存が実際に機能することを確認する。
    from ts_smartcut.models import AppSettings

    settings_manager_module.SettingsManager.save(
        AppSettings(theme="dark", log_enabled=True, log_level="INFO"), settings_path
    )

    windows_seen = []

    def _close_soon():
        for w in QApplication.topLevelWidgets():
            if type(w).__name__ == "MainWindow":
                windows_seen.append(w)
                w.settings.theme = "light"  # セッション中の変更が保存されることを確認する
                w.close()

    QTimer.singleShot(200, _close_soon)

    exit_code = main_module.main()

    assert exit_code == 0
    assert len(windows_seen) == 1
    assert log_path.exists()

    saved = settings_manager_module.SettingsManager.load(settings_path)
    assert saved.theme == "light"  # closeEvent後にmain()が保存した値

    # QOpenGLWidgetのC++側実削除を確定させる(Phase 8で判明した既知の問題への対処)。
    windows_seen[0].deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def test_old_ffmpeg_is_reported_after_start(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QWidget

    from tests._qt_helpers import pump_events
    from ts_smartcut.models import AppSettings
    from ts_smartcut.system.dependency_checker import DependencyChecker

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: shown.append((parent, text)))
    monkeypatch.setattr(DependencyChecker, "old_ffmpeg_version", staticmethod(lambda path=None: "7.1.5"))
    window = QWidget()
    main_module._warn_if_old_ffmpeg(window, AppSettings())
    assert pump_events(qapp, lambda: shown)
    assert shown[0][0] is window and "7.1.5" in shown[0][1] and "8" in shown[0][1]

    shown.clear()
    monkeypatch.setattr(DependencyChecker, "old_ffmpeg_version", staticmethod(lambda path=None: None))
    main_module._warn_if_old_ffmpeg(window, AppSettings())
    assert not pump_events(qapp, lambda: shown, timeout_ms=500)
    window.deleteLater()
