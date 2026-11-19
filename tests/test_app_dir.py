"""設定・記録 DB・ログの置き場所(実行ファイルと同じフォルダ、設計書 §31.9)。"""

import sys

import pytest

from ts_smartcut.core.settings_manager import CONFIG_DIR_ENV, get_config_dir
from ts_smartcut.system.app_dir import AppDirNotWritableError, ensure_writable


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


def test_unwritable_app_folder_is_refused(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(AppDirNotWritableError) as info:
        ensure_writable(blocker / "sub")  # ファイルの下にはフォルダを作れない
    assert info.value.code == "E505"
    ensure_writable(tmp_path / "ok")
    assert list((tmp_path / "ok").iterdir()) == []


def test_prepare_config_dir_makes_the_folder(monkeypatch, tmp_path):
    from ts_smartcut import main

    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "conf"))
    main.prepare_config_dir()
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
