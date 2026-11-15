"""アプリのフォルダ(実行ファイルの横)。設定・記録 DB・ログ・Temp/・KeyframeCache/ を置く。

持ち運べる形にするため、ユーザーの設定フォルダ(%APPDATA%\\ts_smartcut、~/.config/ts_smartcut)
ではなく実行ファイルと同じフォルダに置く(利用者の決定 2026-10-09、設計書 §31.9)。ソースから
動かす時はリポジトリの直下(Linux のランチャーもソースを動かすので同じ)。書き込めない場所
(Program Files 等)に置かれた時は起動しない(OS の一時フォルダなどへの逃げ道は作らない)。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import uuid
from pathlib import Path
from typing import List

from ts_smartcut.errors import SystemResourceError

# 前の版が使っていた場所から移すもの(記録 DB は WAL の横のファイルと一緒に)
LEGACY_FILES = ("settings.json", "recent_files.json", "hardware_encoders.json")
HISTORY_DB_FILES = ("cut_history.db", "cut_history.db-wal", "cut_history.db-shm")
LOG_DIR_NAME = "logs"


class AppDirNotWritableError(SystemResourceError):
    """E505 - 実行ファイルのフォルダに書き込めない(起動しない)。"""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__("E505", f"{path}: {reason}")


class ConfigMigrationError(SystemResourceError):
    """E505 - 前の場所の設定・記録を移せなかった(記録が分かれないよう起動しない)。"""

    def __init__(self, source: Path, target: Path, reason: str):
        self.source = source
        self.target = target
        self.reason = reason
        super().__init__("E505", f"{source} -> {target}: {reason}")


def app_base_dir() -> Path:
    """実行ファイル(.exe)のフォルダ。ソースから動かす時はリポジトリの直下。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def legacy_config_dir() -> Path:
    """2026-10 までの置き場所(OS 標準の設定フォルダの ts_smartcut/)。"""
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ts_smartcut"


def ensure_writable(path: Path) -> None:
    """path(フォルダ)を作り、書き込めるか試す。書き込めなければ AppDirNotWritableError。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_test_{uuid.uuid4().hex}"
        probe.touch()
        probe.unlink()
    except OSError as e:
        raise AppDirNotWritableError(path, str(e)) from e


def migrate_legacy_config(source: Path, target: Path) -> List[str]:
    """前の場所(source)の設定・記録 DB・ログを target へ移す。target に既にあるものは
    移さない(上書きしない)。移したものの名前を返す。ほかのファイル(移行前の DB の
    バックアップ等)はそのまま残す。

    記録 DB は一時的な名前でコピーし終えてから名前を変え、元を消す。途中で失敗したら
    コピーした分を消して ConfigMigrationError(元はそのまま。記録が二つに分かれない)。"""
    if not source.is_dir() or source.resolve() == target.resolve():
        return []
    moved: List[str] = []

    db_sources = [source / name for name in HISTORY_DB_FILES if (source / name).exists()]
    if (source / HISTORY_DB_FILES[0]).exists() and not (target / HISTORY_DB_FILES[0]).exists():
        _move_files_together(db_sources, target)
        moved.extend(p.name for p in db_sources)

    for name in LEGACY_FILES:
        if (source / name).exists() and not (target / name).exists():
            _move_files_together([source / name], target)
            moved.append(name)

    old_logs, new_logs = source / LOG_DIR_NAME, target / LOG_DIR_NAME
    if old_logs.is_dir() and not new_logs.exists():
        try:
            shutil.copytree(old_logs, new_logs)
        except OSError as e:
            shutil.rmtree(new_logs, ignore_errors=True)
            raise ConfigMigrationError(old_logs, new_logs, str(e)) from e
        shutil.rmtree(old_logs, ignore_errors=True)
        moved.append(LOG_DIR_NAME)
    return moved


def _move_files_together(files: List[Path], target: Path) -> None:
    """files を target へ移す。全部コピーできてから名前を変えて元を消す。"""
    staged = []
    try:
        for path in files:
            temp = target / f".{path.name}.moving"
            shutil.copy2(path, temp)
            staged.append((path, temp))
        for path, temp in staged:
            temp.replace(target / path.name)
    except OSError as e:
        for path, temp in staged:
            temp.unlink(missing_ok=True)
            if (target / path.name).exists() and path.exists():
                (target / path.name).unlink(missing_ok=True)
        raise ConfigMigrationError(files[0], target, str(e)) from e
    for path, _temp in staged:
        try:
            path.unlink()
        except OSError:
            pass  # 新しい場所に揃っているので使える(古い方は利用者が消せる)
