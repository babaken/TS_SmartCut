import logging

from ts_smartcut.logging_setup import (
    BACKUP_COUNT,
    LOGGER_NAME,
    MAX_BYTES,
    TRACE_LEVEL,
    configure_logging,
)
from ts_smartcut.models import AppSettings


def test_configure_logging_creates_rotating_file_handler(tmp_path):
    log_path = tmp_path / "app.log"
    settings = AppSettings(log_enabled=True, log_level="INFO")

    logger = configure_logging(settings, log_path=log_path)

    assert logger.name == LOGGER_NAME
    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == MAX_BYTES
    assert handler.backupCount == BACKUP_COUNT
    assert logger.level == logging.INFO


def test_configure_logging_writes_to_file(tmp_path):
    log_path = tmp_path / "app.log"
    settings = AppSettings(log_enabled=True, log_level="INFO")
    logger = configure_logging(settings, log_path=log_path)

    logger.info("テストメッセージ")
    for handler in logger.handlers:
        handler.flush()

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "テストメッセージ" in content
    assert "[INFO]" in content


def test_configure_logging_disabled_uses_null_handler(tmp_path):
    log_path = tmp_path / "app.log"
    settings = AppSettings(log_enabled=False)

    logger = configure_logging(settings, log_path=log_path)

    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.NullHandler)
    assert not log_path.exists()


def test_configure_logging_respects_log_level(tmp_path):
    log_path = tmp_path / "app.log"
    settings = AppSettings(log_enabled=True, log_level="ERROR")
    logger = configure_logging(settings, log_path=log_path)

    logger.info("これは記録されないはず")
    logger.error("これは記録されるはず")
    for handler in logger.handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "これは記録されないはず" not in content
    assert "これは記録されるはず" in content


def test_configure_logging_supports_trace_level(tmp_path):
    log_path = tmp_path / "app.log"
    settings = AppSettings(log_enabled=True, log_level="TRACE")
    logger = configure_logging(settings, log_path=log_path)

    assert logger.level == TRACE_LEVEL
    logger.log(TRACE_LEVEL, "トレースメッセージ")
    for handler in logger.handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "トレースメッセージ" in content
    assert "[TRACE]" in content


def test_reconfigure_clears_previous_handlers(tmp_path):
    log_path1 = tmp_path / "app1.log"
    log_path2 = tmp_path / "app2.log"
    settings = AppSettings(log_enabled=True)

    configure_logging(settings, log_path=log_path1)
    logger = configure_logging(settings, log_path=log_path2)

    assert len(logger.handlers) == 1


def test_child_logger_propagates_to_configured_handler(tmp_path):
    """ts_smartcut.* 配下のモジュールがlogging.getLogger(__name__)で
    取得するロガーが、ここで設定したハンドラへ自動的に伝播することを
    確認する（design doc §14、モジュール側の追加設定が不要である根拠）。"""
    log_path = tmp_path / "app.log"
    settings = AppSettings(log_enabled=True, log_level="INFO")
    configure_logging(settings, log_path=log_path)

    child_logger = logging.getLogger("ts_smartcut.core.some_module")
    child_logger.info("子ロガーからのメッセージ")
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "子ロガーからのメッセージ" in content


def test_uncaught_exceptions_in_threads_and_qt_slots_are_logged(tmp_path):
    """コンソールの無い起動では捨てられていた予期しない例外が、ログに残る(2026-10-12)。
    フックを書き換えるので別のプロセスで確かめる。"""
    import subprocess
    import sys
    import textwrap

    log_path = tmp_path / "app.log"
    script = textwrap.dedent(f"""
        import sys, threading
        from pathlib import Path
        from PySide6.QtCore import QObject, QTimer, Signal
        from PySide6.QtWidgets import QApplication
        from ts_smartcut.logging_setup import configure_logging, install_exception_logging
        from ts_smartcut.models import AppSettings

        sys.stderr = None  # pythonw.exe と同じ
        configure_logging(AppSettings(log_enabled=True, log_level="INFO"), log_path=Path({str(log_path)!r}))
        install_exception_logging()

        def fail_in_thread():
            raise ValueError("thread boom")
        thread = threading.Thread(target=fail_in_thread, name="worker-x")
        thread.start()
        thread.join()

        class Emitter(QObject):
            fired = Signal()
        def fail_in_slot():
            raise RuntimeError("slot boom")
        app = QApplication([])
        emitter = Emitter()
        emitter.fired.connect(fail_in_slot)
        QTimer.singleShot(0, emitter.fired.emit)
        QTimer.singleShot(50, app.quit)
        app.exec()
        print("still running")
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert "still running" in result.stdout, result.stderr
    content = log_path.read_text(encoding="utf-8")
    assert "Uncaught exception in thread worker-x" in content
    assert "ValueError: thread boom" in content
    assert "RuntimeError: slot boom" in content
    assert "fail_in_slot" in content  # スタックトレースも残る
