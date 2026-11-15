"""mpv統合パッケージ (design doc §2、render API改訂版)。

mpvはソフト依存 — 未導入・libmpv不在でもアプリは起動でき、カット編集
機能はこのパッケージに依存せず動作し続ける（design doc §12.2、
CLAUDE.md）。create_controller()は接続に失敗した場合Noneを返すため、
呼び出し側はプレビュー制限モードにフォールバックすること。

render APIはOSごとの「ウィンドウ埋め込み」実装を必要としないため、
単一のMPVController実装をそのまま返す（design doc §2、CLAUDE.md — OS
固有の問題が実際に見つかった場合のみ分割する）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ts_smartcut.errors import MPVError
from ts_smartcut.mpv import libmpv
from ts_smartcut.mpv.base import MPVControllerBase
from ts_smartcut.mpv.controller import MPVController


def is_mpv_available() -> bool:
    """libmpv を読み込めるかを確認する(結び付けは自作の libmpv.py。設計書 §2.1)。"""
    return libmpv.is_available()


def create_controller(
    get_proc_address: Callable[[bytes], int], **extra_mpv_options: Any
) -> Optional[MPVControllerBase]:
    """MPVControllerを生成・接続する。失敗時はNoneを返す
    （呼び出し側はプレビュー制限モードへフォールバックすること）。
    get_proc_addressは呼び出し側のOpenGLコンテキストが有効な状態で
    呼ぶこと（例: QOpenGLWidget.initializeGL()の中）。"""
    controller: MPVControllerBase = MPVController(**extra_mpv_options)
    try:
        controller.connect(get_proc_address)
    except MPVError:
        return None
    return controller


__all__ = ["MPVControllerBase", "MPVController", "is_mpv_available", "create_controller"]
