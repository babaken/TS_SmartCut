"""「最近使用したファイル」メニュー (design doc §4.2, §10.4)。

windowフィクスチャ(module scope)はtests/conftest.pyで定義されている。
テストは定義順に状態を引き継ぐ（同一MainWindowインスタンスを共有）。"""

import shutil

from PySide6.QtWidgets import QFileDialog, QMessageBox

import pytest


def _menu_texts(window):
    window._rebuild_recent_menu()
    return [a.text() for a in window.recent_menu.actions() if not a.isSeparator()]


@pytest.fixture
def captured_warnings(monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    return warnings


def test_recent_files_stored_outside_real_config(window, isolated_config_dir):
    assert window.recent_files.path.is_relative_to(isolated_config_dir)


def test_empty_history_shows_disabled_placeholder(window):
    window.recent_files.clear()
    window._rebuild_recent_menu()
    actions = [a for a in window.recent_menu.actions() if not a.isSeparator()]

    assert [a.text() for a in actions] == ["（履歴なし）", "履歴をクリア"]
    assert all(not a.isEnabled() for a in actions)


def test_open_via_dialog_adds_to_history(window, interlaced_sample_video, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(interlaced_sample_video), ""))

    window._on_open()

    assert window.recent_files.files == [interlaced_sample_video]
    assert _menu_texts(window) == [str(interlaced_sample_video), "履歴をクリア"]


def test_save_project_adds_project_to_front(window, interlaced_sample_video, tmp_path):
    window.cut_manager.add_cut(10, 20)
    project = tmp_path / "p.json"

    window.save_project(project)

    assert window.recent_files.files == [project, interlaced_sample_video]


def test_open_recent_project_restores_cuts(window, interlaced_sample_video):
    project = window.recent_files.files[0]
    window.open_file(interlaced_sample_video)
    assert window.cut_manager.cuts == []

    window._rebuild_recent_menu()
    triggered = [a for a in window.recent_menu.actions() if a.text() == str(project)]
    triggered[0].trigger()

    assert [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts] == [(10, 20)]
    assert window._project_path == project


def test_open_recent_video_moves_it_to_front(window, interlaced_sample_video):
    assert window.open_recent_file(interlaced_sample_video) is True

    assert window.recent_files.files[0] == interlaced_sample_video


def test_missing_file_is_removed_with_warning(window, tmp_path, captured_warnings):
    gone = tmp_path / "gone.mp4"
    window.recent_files.add(gone)

    assert window.open_recent_file(gone) is False

    assert len(captured_warnings) == 1
    assert gone not in window.recent_files.files


def test_menu_drops_files_deleted_since_last_shown(window, interlaced_sample_video, tmp_path):
    copy = tmp_path / "copy.mp4"
    shutil.copy(interlaced_sample_video, copy)
    window.recent_files.add(copy)
    assert str(copy) in _menu_texts(window)

    copy.unlink()

    assert str(copy) not in _menu_texts(window)


def test_clear_history(window):
    window._on_clear_recent_files()

    assert window.recent_files.files == []
    assert _menu_texts(window) == ["（履歴なし）", "履歴をクリア"]
