"""Temp file lifecycle management (design doc §11)."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterator, List, Optional

from ts_smartcut.system.app_dir import app_base_dir

logger = logging.getLogger(__name__)

RUN_DIR_PREFIX = "run_"
_LOCK_NAME = ".lock"
# アプリの一時フォルダ(実行ファイルの直下)の名前
APP_TEMP_DIR_NAME = "Temp"
# キーフレームのキャッシュのフォルダ(実行ファイルの直下)の名前
KEYFRAME_CACHE_DIR_NAME = "KeyframeCache"


def app_temp_dir() -> Path:
    """アプリの一時フォルダ。実行ファイル(.exe)の直下の Temp/(ソースから動かす時は
    リポジトリの直下)。OS の一時フォルダは使わない(Linux の /tmp は小さい tmpfs の
    ことが多い。利用者の指示 2026-10-08)。書き込めない場所ではアプリが起動しない
    (main.py、設計書 §31.9)ので、逃げ道は無い。"""
    return _app_dir(APP_TEMP_DIR_NAME)


def keyframe_cache_dir() -> Path:
    """キーフレームのキャッシュのフォルダ。実行ファイルの直下の KeyframeCache/(ソースから
    動かす時はリポジトリの直下)。録画の隣には置かない(読み取り専用の共有の録画も開ける
    ように、録画のフォルダを汚さないように。利用者の決定 2026-10-09、設計書 §26.1)。"""
    return _app_dir(KEYFRAME_CACHE_DIR_NAME)


def _app_dir(name: str) -> Path:
    path = app_base_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class StaleTempSummary:
    count: int
    total_bytes: int


class TempFileManager:
    def __init__(self, temp_dir: Optional[Path] = None):
        self.temp_dir = temp_dir or Path(tempfile.gettempdir()) / "ts_smartcut"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.active_files: List[Path] = []

    def create_temp_file(self, suffix: str = ".mp4") -> Path:
        """一時ファイルパスを発行する（ファイル自体はここでは作成しない。
        呼び出し側、例えばffmpegの出力先として使用する想定）。"""
        filename = f"temp_{uuid.uuid4().hex}{suffix}"
        temp_path = self.temp_dir / filename
        self.active_files.append(temp_path)
        return temp_path

    def cleanup(self) -> None:
        """このインスタンスが発行した一時ファイルを全て削除する。"""
        for path in self.active_files:
            if path.exists():
                path.unlink()
        self.active_files.clear()


# ---- 処理ごとの一時フォルダと、前回の残りの削除 (design doc §11.2) ----------------
# 1回の処理(1ファイル)ごとに base/run_<id>/ を作り、その中の .lock をOSの排他ロックで
# 開いたままにする。アプリが異常終了したりPCが落ちたりするとロックはOSが外すので、
# ロックを取れる run_ フォルダは「使っている処理がもう無い残り」と判断できる。
# 経過時間で判断すると、長い処理の途中のファイルや、同時に動いている別の
# アプリの一時ファイルまで消してしまう恐れがある。

def _try_lock(handle: IO[bytes]) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _tree_size(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file() and p.name != _LOCK_NAME]
    return len(files), sum(p.stat().st_size for p in files)


def remove_stale_temp_files(base: Path) -> StaleTempSummary:
    """baseに残っている前回の一時ファイルを削除し、削除した件数と合計サイズを返す。
    使用中の処理(ロック中)のフォルダには触れない。"""
    count = total = 0
    if not base.is_dir():
        return StaleTempSummary(0, 0)
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith(RUN_DIR_PREFIX):
            lock_path = entry / _LOCK_NAME
            try:
                with open(lock_path, "ab") as handle:
                    if not _try_lock(handle):
                        continue  # 使用中
                    n, size = _tree_size(entry)
                    _unlock(handle)
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            if entry.exists():
                # Windowsでは書き込み中のファイルは消せない(異常終了したアプリの
                # ffmpegがまだ動いている等)。消せた分だけ数え、残りは次回に消す。
                left_n, left_size = _tree_size(entry)
                logger.warning("Could not delete some leftover temporary files (in use): %d (%s)", left_n, entry)
                n, size = n - left_n, size - left_size
            count, total = count + n, total + size
    if count:
        logger.info("Deleted leftover temporary files: %d, %.1f MB in total (%s)", count, total / 1e6, base)
    return StaleTempSummary(count, total)


@contextmanager
def locked_run_dir(base: Path) -> Iterator[Path]:
    """この処理専用の一時フォルダ(base/run_<id>)を作り、使っている間ロックする。
    抜ける時に中身ごと削除する。"""
    run_dir = base / f"{RUN_DIR_PREFIX}{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True)
    handle = open(run_dir / _LOCK_NAME, "ab")
    try:
        if not _try_lock(handle):
            raise OSError(f"Cannot lock the temporary folder: {run_dir}")
        yield run_dir
    finally:
        _unlock(handle)
        handle.close()
        shutil.rmtree(run_dir, ignore_errors=True)
