"""アプリケーションエントリーポイント (design doc §0)。

起動時に設定を読み込み、ログを設定してからGUIを起動する。終了時には
設定を保存する。"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ts_smartcut.core.hardware_encoders import get_hardware_encoders
from ts_smartcut.core.settings_manager import CONFIG_DIR_ENV, SettingsManager, get_config_dir
from ts_smartcut.ffmpeg.wrapper import remove_old_keyframe_caches
from ts_smartcut.logging_setup import LOGGER_NAME, configure_logging, install_exception_logging
from ts_smartcut.system.app_dir import (
    AppDirNotWritableError,
    ConfigMigrationError,
    ensure_writable,
    legacy_config_dir,
    migrate_legacy_config,
)
from ts_smartcut.system.child_processes import forget_bundled_qt_paths, restore_library_path_for_children

logger = logging.getLogger(__name__)


def _resource_icon_path() -> Path:
    """アプリアイコン(PNG)のパスを返す。PyInstallerでフリーズした場合は
    language_manager.pyと同じ理由でsys._MEIPASS基準に切り替える。"""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).parent
    return base / "gui" / "resources" / "icon.png"


def prepare_config_dir() -> list:
    """設定・記録 DB・ログのフォルダ(実行ファイルと同じフォルダ)を使えるようにする。
    書き込めなければ AppDirNotWritableError。前の版の場所(OS 標準の設定フォルダ)に
    あるものは移す(移したものの名前を返す。失敗は ConfigMigrationError)。"""
    config_dir = get_config_dir()
    ensure_writable(config_dir)
    if os.environ.get(CONFIG_DIR_ENV):
        return []  # テスト等で場所を変えている時は、利用者の設定に触らない
    return migrate_legacy_config(legacy_config_dir(), config_dir)


def _show_startup_error(error: Exception) -> None:
    """起動できない理由を出す(設定・言語ファイルを読めなくても日本語で出す)。"""
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.i18n.language_manager import LanguageManager

    settings_path = get_config_dir() / "settings.json"
    if not settings_path.exists():
        settings_path = legacy_config_dir() / "settings.json"
    try:
        lang = LanguageManager(SettingsManager.load(settings_path).language)
    except Exception:
        lang = LanguageManager()
    if isinstance(error, AppDirNotWritableError):
        text = lang.get("messages.app_dir_not_writable", path=error.path, code=error.code, reason=error.reason)
    else:
        text = lang.get("messages.config_migration_failed", source=error.source, target=error.target,
                        code=error.code, reason=error.reason)
    _app = QApplication.instance() or QApplication(sys.argv)  # メッセージの表示に必要
    QMessageBox.critical(None, lang.get("messages.startup_error_title"), text)


def main() -> int:
    restore_library_path_for_children()
    try:
        moved = prepare_config_dir()
    except (AppDirNotWritableError, ConfigMigrationError) as e:
        _show_startup_error(e)
        return 1
    settings = SettingsManager.load()
    configure_logging(settings)
    install_exception_logging()
    if moved:
        logging.getLogger(LOGGER_NAME).info(
            "Moved settings and history from %s to %s: %s", legacy_config_dir(), get_config_dir(), ", ".join(moved))
    # ハードウェアエンコーダーの判定(初回起動時のみ実際に判定し、結果を保存する)。
    # 画面の表示を待たせないよう裏で行う。処理開始時に未完了なら完了を待つ。
    threading.Thread(target=get_hardware_encoders, args=(settings,), daemon=True).start()
    # 最後に使ってから 30 日たったキーフレームのキャッシュを消す(設計書 §26.1)
    threading.Thread(target=remove_old_keyframe_caches, daemon=True).start()
    logging.getLogger(LOGGER_NAME).info("TS_SmartCut started")

    # Windowsのタスクバーは、プロセスにAppUserModelIDが無いと起動した実行ファイル
    # (pythonw.exe)の仲間として扱い、Pythonのアイコンを出す(2026-10-06確認)。
    # 独自のIDを付けると、setWindowIcon()のアイコンが出る。ウィンドウを作る前に呼ぶ。
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("babaken.TS_SmartCut")
        except (OSError, AttributeError):
            pass

    app = QApplication.instance() or QApplication(sys.argv)
    forget_bundled_qt_paths()
    # Linux(X11/Wayland)でタスクバー/ウィンドウ切替のアイコンを正しく表示する
    # ために必要。特にWaylandはapp_idと.desktopファイル名(拡張子抜き)の一致を
    # 見てKWin側がアイコンを解決するため、setWindowIcon()だけでは不十分
    # だった(実機で確認: setDesktopFileName()なしだと汎用フォールバック
    # アイコンになる)。
    app.setDesktopFileName("ts_smartcut")
    icon_path = _resource_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    from ts_smartcut.gui.main_window import MainWindow

    window = MainWindow(settings)
    window.show()
    _warn_if_native_missing(window)
    _warn_if_old_ffmpeg(window, settings)

    exit_code = app.exec()

    # セッション中の変更（設定ダイアログ経由以外、例: テーマ切替メニュー）
    # も含めて最終状態を保存する。
    SettingsManager.save(window.settings)
    logging.getLogger(LOGGER_NAME).info("TS_SmartCut exited")
    return exit_code


def _warn_if_native_missing(window) -> None:
    """自動カットの計算の部品(ts_smartcut_native)が使えなければ、起動の後に知らせる(E506)。
    カットの編集・変換はできるので起動は止めない。"""
    from ts_smartcut.autocut.native import native_problem

    problem = native_problem()
    if problem is None:
        return
    logging.getLogger(LOGGER_NAME).warning("%s", problem.log_message)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.i18n.language_manager import tr

    QTimer.singleShot(0, lambda: QMessageBox.warning(window, tr("errors.E506"), problem.message))


def _warn_if_old_ffmpeg(window, settings) -> None:
    """ffmpeg が必要な版(MIN_FFMPEG_MAJOR、今は 6)より古ければ、起動の後に知らせる。版は裏で調べる(起動を待たせない)。
    古くても動く所はあるので起動は止めない(2026-11-16、利用者の決定)。"""
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.i18n.language_manager import tr
    from ts_smartcut.ffmpeg.compat import MIN_FFMPEG_MAJOR
    from ts_smartcut.system.dependency_checker import DependencyChecker

    class _Bridge(QObject):
        found = Signal(str)

    bridge = _Bridge(window)  # 窓の子: GUI のスレッドで受け、窓と一緒に消える
    bridge.found.connect(lambda version: QMessageBox.warning(
        window, tr("messages.ffmpeg_old_title"),
        tr("messages.ffmpeg_old", version=version, required=MIN_FFMPEG_MAJOR),
    ))

    def check() -> None:
        version = DependencyChecker.old_ffmpeg_version(settings.ffmpeg_path)
        if version is None:
            return
        logging.getLogger(LOGGER_NAME).warning(
            "ffmpeg %s is older than the required %d; some processing may go wrong", version, MIN_FFMPEG_MAJOR)
        try:
            bridge.found.emit(version)
        except RuntimeError:
            pass  # 調べ終わる前に窓が閉じた

    threading.Thread(target=check, daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
