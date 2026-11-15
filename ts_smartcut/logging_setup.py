"""ログ設計 (design doc §14)。

`logging.getLogger(__name__)`で取得されるパッケージ内の各ロガー
（例: `ts_smartcut.core.batch_manager`）は、標準のロガー階層により
ここで設定する`"ts_smartcut"`ロガーへ自動的に伝播する。個々のモジュール
側で追加設定は不要。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

from ts_smartcut.core.settings_manager import get_config_dir
from ts_smartcut.models import AppSettings

LOGGER_NAME = "ts_smartcut"

TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")

MAX_BYTES = 10 * 1024 * 1024  # 10MB (design doc §14.4)
BACKUP_COUNT = 5  # 保持世代数 (design doc §14.4)

_LEVEL_NAMES = {
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": TRACE_LEVEL,
}


def get_default_log_path() -> Path:
    """設定のフォルダの logs/ にログファイルを置く（core/settings_manager.py の get_config_dir）。"""
    return get_config_dir() / "logs" / "ts_smartcut.log"


def configure_logging(settings: AppSettings, log_path: Optional[Path] = None) -> logging.Logger:
    """design doc §14に基づきロガーを設定する。settings.log_enabledが
    Falseの場合は何も出力しない（NullHandlerのみ）。既存のハンドラは
    再設定のたびにクリアする（テストや設定変更時の再呼び出しに対応）。"""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = False

    if not settings.log_enabled:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        return logger

    log_path = log_path or get_default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    level = _LEVEL_NAMES.get(settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)

    return logger


def install_exception_logging() -> None:
    """予期しない例外をログに書く。コンソールの無い起動(pythonw.exe・console=False の exe)では
    Qt のスロットや裏のスレッドで出た例外が stderr に捨てられ、何も残らなかった
    (2026-10-12: 波形が描かれず案を承認できない現象で原因が追えなかった)。
    Qt のスロットの例外は sys.excepthook に、スレッドの例外は threading.excepthook に来る。"""
    logger = logging.getLogger(LOGGER_NAME)
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def log_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        if sys.stderr is not None:
            previous_hook(exc_type, exc_value, exc_traceback)

    def log_thread_exception(args) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        logger.critical("Uncaught exception in thread %s", name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        if sys.stderr is not None:
            previous_thread_hook(args)

    sys.excepthook = log_exception
    threading.excepthook = log_thread_exception
