"""プロジェクト保存・読込のメニュー接続 (design doc §10)。

windowフィクスチャ(module scope)はtests/conftest.pyで定義されている。
テストは定義順に状態を引き継ぐ（同一MainWindowインスタンスを共有）。"""

import json

from PySide6.QtWidgets import QFileDialog, QMessageBox

import pytest

from ts_smartcut.core.project_manager import CURRENT_PROJECT_VERSION


@pytest.fixture
def captured_dialogs(monkeypatch):
    calls = {"information": [], "warning": [], "critical": []}
    for kind in calls:
        monkeypatch.setattr(QMessageBox, kind, lambda *a, _k=kind, **kw: calls[_k].append(a))
    return calls


def test_save_actions_disabled_before_file_loaded(window):
    assert window.save_project_action.isEnabled() is False
    assert window.save_project_as_action.isEnabled() is False


def test_save_without_file_shows_message(window, captured_dialogs):
    window._on_save_project()
    assert len(captured_dialogs["information"]) == 1


def test_save_project_writes_cuts_and_bookmarks(window, interlaced_sample_video, tmp_path):
    assert window.open_file(interlaced_sample_video) is True
    assert window.save_project_action.isEnabled() is True
    window.cut_manager.add_cut(10, 20, "CM1")
    window.cut_manager.add_cut(40, 50)
    window.bookmark_manager.add_bookmark(30, "CM開始")

    path = tmp_path / "project.json"
    assert window.save_project(path) is True

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == CURRENT_PROJECT_VERSION
    assert data["file_info"]["file_path"] == str(interlaced_sample_video)
    assert [(c["start_frame"], c["end_frame"], c["label"]) for c in data["file_info"]["cuts"]] == [
        (10, 20, "CM1"),
        (40, 50, None),
    ]
    assert [(b["frame"], b["name"]) for b in data["file_info"]["bookmarks"]] == [(30, "CM開始")]
    assert data["file_info"]["keyframes"] is None
    assert window._project_path == path


def test_ctrl_s_after_save_overwrites_same_path(window, tmp_path, monkeypatch):
    path = window._project_path
    created_at = window._project_created_at
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **k: pytest.fail("保存済みなのにダイアログが出た")
    )
    window.cut_manager.add_cut(60, 70)

    window._on_save_project()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["file_info"]["cuts"]) == 3
    assert data["created_at"] == created_at.isoformat()


def test_open_project_restores_cuts_and_bookmarks_without_undo_history(window, interlaced_sample_video, captured_dialogs):
    path = window._project_path
    # 別の状態にしてから読み込み直す
    window.open_file(interlaced_sample_video)
    assert window.cut_manager.cuts == []
    assert window._project_path is None

    assert window.open_project(path) is True

    assert [(c.start_frame, c.end_frame, c.label) for c in window.cut_manager.cuts] == [
        (10, 20, "CM1"),
        (40, 50, None),
        (60, 70, None),
    ]
    assert window.cut_list.topLevelItemCount() == 3
    assert [(b.frame, b.name) for b in window.bookmark_manager.bookmarks] == [(30, "CM開始")]
    assert window.bookmark_list.topLevelItemCount() == 1
    assert window.cut_manager.can_undo is False
    assert window._project_path == path
    assert captured_dialogs["warning"] == []
    assert captured_dialogs["critical"] == []


def test_open_project_skips_invalid_cuts_and_warns_on_changed_file(window, tmp_path, captured_dialogs):
    data = json.loads(window._project_path.read_text(encoding="utf-8"))
    data["file_info"]["file_size"] += 1
    data["file_info"]["cuts"].append({"start_frame": 70, "end_frame": 9999, "label": None})
    data["file_info"]["bookmarks"].append({"frame": 9999, "name": "範囲外", "timestamp": "2026-10-04T00:00:00"})
    path = tmp_path / "modified.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert window.open_project(path) is True

    assert len(window.cut_manager.cuts) == 3
    assert len(window.bookmark_manager.bookmarks) == 1
    # 「ファイル変更の可能性」と「区間スキップ」の2件
    assert len(captured_dialogs["warning"]) == 2


def test_open_project_with_missing_video_shows_error(window, tmp_path, captured_dialogs):
    data = json.loads(window._project_path.read_text(encoding="utf-8"))
    data["file_info"]["file_path"] = str(tmp_path / "missing.mp4")
    path = tmp_path / "missing_video.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    cuts_before = list(window.cut_manager.cuts)

    assert window.open_project(path) is False

    assert len(captured_dialogs["critical"]) == 1
    assert window.cut_manager.cuts == cuts_before


def test_open_corrupt_project_shows_error(window, tmp_path, captured_dialogs):
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")

    assert window.open_project(path) is False

    assert len(captured_dialogs["critical"]) == 1
    assert "E401" in captured_dialogs["critical"][0][2]


def test_save_as_appends_json_suffix(window, interlaced_sample_video, tmp_path, monkeypatch):
    target = tmp_path / "sample.mp4"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))

    window._on_save_project_as()

    assert window._project_path == tmp_path / "sample.mp4.json"
    assert window._project_path.exists()
    assert not target.exists()
