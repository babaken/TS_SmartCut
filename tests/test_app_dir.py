"""設定・記録 DB・ログの置き場所(実行ファイルと同じフォルダ、設計書 §31.9)。"""

import sqlite3
import sys

import pytest

from ts_smartcut.core.settings_manager import CONFIG_DIR_ENV, get_config_dir
from ts_smartcut.system import app_dir
from ts_smartcut.system.app_dir import (
    AppDirNotWritableError,
    ConfigMigrationError,
    ensure_writable,
    migrate_legacy_config,
)


def test_config_dir_is_the_executable_folder_by_default(monkeypatch, tmp_path):
    exe = tmp_path / "app" / "TS_SmartCut.exe"
    exe.parent.mkdir()
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert get_config_dir() == exe.parent


def test_config_dir_from_source_is_the_repository_root(monkeypatch):
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = get_config_dir()
    assert (root / "ts_smartcut" / "main.py").exists()


def _legacy(tmp_path):
    old = tmp_path / "old"
    (old / "logs").mkdir(parents=True)
    (old / "settings.json").write_text('{"language": "en"}', encoding="utf-8")
    (old / "recent_files.json").write_text("[]", encoding="utf-8")
    (old / "hardware_encoders.json").write_text("{}", encoding="utf-8")
    (old / "logs" / "ts_smartcut.log").write_text("old log\n", encoding="utf-8")
    (old / "logs" / "ts_smartcut.log.1").write_text("older log\n", encoding="utf-8")
    (old / "cut_history_v5_backup_20261011.db").write_bytes(b"backup")
    db = sqlite3.connect(old / "cut_history.db")
    db.execute("create table t (x)")
    db.execute("insert into t values (42)")
    db.commit()
    db.close()
    return old


def test_migration_moves_settings_history_and_logs(tmp_path):
    old = _legacy(tmp_path)
    new = tmp_path / "new"
    new.mkdir()
    moved = migrate_legacy_config(old, new)
    assert set(moved) >= {"cut_history.db", "settings.json", "recent_files.json", "hardware_encoders.json", "logs"}
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"language": "en"}'
    assert (new / "logs" / "ts_smartcut.log.1").read_text(encoding="utf-8") == "older log\n"
    db = sqlite3.connect(new / "cut_history.db")
    assert db.execute("select x from t").fetchone() == (42,)
    db.close()
    for name in ("settings.json", "recent_files.json", "hardware_encoders.json", "cut_history.db", "logs"):
        assert not (old / name).exists()
    # 移行前の DB のバックアップ等、ほかのものは残す(利用者が消す)
    assert (old / "cut_history_v5_backup_20261011.db").read_bytes() == b"backup"
    assert not list(new.glob(".*.moving"))
    # 2 回目は何もしない
    assert migrate_legacy_config(old, new) == []


def test_migration_never_overwrites_what_is_already_there(tmp_path):
    old = _legacy(tmp_path)
    new = tmp_path / "new"
    new.mkdir()
    (new / "settings.json").write_text('{"language": "jp"}', encoding="utf-8")
    (new / "cut_history.db").write_bytes(b"newer")
    moved = migrate_legacy_config(old, new)
    assert "settings.json" not in moved and "cut_history.db" not in moved
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"language": "jp"}'
    assert (new / "cut_history.db").read_bytes() == b"newer"
    assert (old / "settings.json").exists() and (old / "cut_history.db").exists()


def test_migration_without_a_legacy_folder_does_nothing(tmp_path):
    assert migrate_legacy_config(tmp_path / "missing", tmp_path) == []


def test_failed_history_migration_keeps_the_original_and_leaves_nothing_behind(tmp_path, monkeypatch):
    old = _legacy(tmp_path)
    new = tmp_path / "new"
    new.mkdir()
    real_copy = app_dir.shutil.copy2

    def failing_copy(src, dst, *a, **k):
        if str(src).endswith("cut_history.db"):
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(app_dir.shutil, "copy2", failing_copy)
    with pytest.raises(ConfigMigrationError) as info:
        migrate_legacy_config(old, new)
    assert info.value.code == "E505"
    assert (old / "cut_history.db").exists()
    assert not (new / "cut_history.db").exists()
    assert not list(new.glob(".*.moving"))


def test_unwritable_app_folder_is_refused(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(AppDirNotWritableError) as info:
        ensure_writable(blocker / "sub")  # ファイルの下にはフォルダを作れない
    assert info.value.code == "E505"
    ensure_writable(tmp_path / "ok")
    assert list((tmp_path / "ok").iterdir()) == []


def test_prepare_config_dir_does_not_touch_the_users_folder_under_the_test_override(monkeypatch, tmp_path):
    from ts_smartcut import main

    monkeypatch.setattr(app_dir, "legacy_config_dir", lambda: pytest.fail("legacy folder read"))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "conf"))
    assert main.prepare_config_dir() == []
    assert (tmp_path / "conf").is_dir()


def test_startup_error_message_names_the_folder(monkeypatch, tmp_path):
    """書き込めない時は理由と場所を出して起動しない(メッセージの文は言語ファイルから)。"""
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut import main

    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: shown.append((title, text)))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "conf"))
    main._show_startup_error(AppDirNotWritableError(tmp_path / "Program Files" / "TS_SmartCut", "Access denied"))
    title, text = shown[0]
    assert "TS_SmartCut" in title
    assert str(tmp_path / "Program Files" / "TS_SmartCut") in text and "[E505] Access denied" in text
    main._show_startup_error(ConfigMigrationError(tmp_path / "old", tmp_path / "new", "locked"))
    assert "[E505] locked" in shown[1][1] and str(tmp_path / "old") in shown[1][1]
