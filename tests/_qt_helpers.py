"""Qt6テスト共通ヘルパー。conftest.pyのfixtureからではなく通常の
importで使う（複数のテストファイルで共有するため）。"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QCoreApplication


def pump_events(
    app: QCoreApplication,
    condition: Callable[[], bool],
    timeout_ms: int = 3000,
    interval_ms: int = 20,
) -> bool:
    """conditionがTrueになるかtimeout_msに達するまでイベントループを
    回す。mpvのrender API・非同期シーク等、Qtのイベント処理を挟まないと
    反映されない状態を待つのに使う。戻り値はconditionが満たされたか。"""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(interval_ms / 1000)
    app.processEvents()
    return condition()
