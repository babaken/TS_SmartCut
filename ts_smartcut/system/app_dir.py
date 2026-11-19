"""アプリのフォルダ(実行ファイルの横)。設定・記録 DB・ログ・Temp/・KeyframeCache/ を置く。

持ち運べる形にするため、ユーザーの設定フォルダ(%APPDATA%\\ts_smartcut、~/.config/ts_smartcut)
ではなく実行ファイルと同じフォルダに置く(利用者の決定 2026-10-09、設計書 §31.9)。ソースから
動かす時はリポジトリの直下(Linux のランチャーもソースを動かすので同じ)。書き込めない場所
(Program Files 等)に置かれた時は起動しない(OS の一時フォルダなどへの逃げ道は作らない)。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from ts_smartcut.errors import SystemResourceError


class AppDirNotWritableError(SystemResourceError):
    """E505 - 実行ファイルのフォルダに書き込めない(起動しない)。"""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__("E505", f"{path}: {reason}")


def app_base_dir() -> Path:
    """実行ファイル(.exe)のフォルダ。ソースから動かす時はリポジトリの直下。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def ensure_writable(path: Path) -> None:
    """path(フォルダ)を作り、書き込めるか試す。書き込めなければ AppDirNotWritableError。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_test_{uuid.uuid4().hex}"
        probe.touch()
        probe.unlink()
    except OSError as e:
        raise AppDirNotWritableError(path, str(e)) from e
