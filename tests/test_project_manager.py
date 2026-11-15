import json
from datetime import datetime
from pathlib import Path

import pytest

from ts_smartcut.core.project_manager import CURRENT_PROJECT_VERSION, ProjectManager
from ts_smartcut.errors import ProjectError
from ts_smartcut.models import AppSettings, CutRange, FileCutInfo, Project


def _sample_project() -> Project:
    file_info = FileCutInfo(
        file_path=Path("/videos/sample.mp4"),
        total_frames=54000,
        fps=29.97,
        resolution=(1920, 1080),
        video_codec="h264",
        audio_codec="aac",
        bitrate=5_000_000,
        duration=1801.8,
        file_size=1_234_567_890,
        cuts=[CutRange(1000, 5000, "OP")],
    )
    return Project(
        version=CURRENT_PROJECT_VERSION,
        file_info=file_info,
        settings=AppSettings(),
        created_at=datetime(2026, 2, 12, 10, 30, 0),
        modified_at=datetime(2026, 2, 12, 11, 45, 0),
    )


def test_save_and_load_round_trip(tmp_path):
    project = _sample_project()
    path = tmp_path / "project.json"

    ProjectManager.save_project(project, path)
    loaded = ProjectManager.load_project(path)

    assert loaded == project


def test_save_writes_readable_json(tmp_path):
    project = _sample_project()
    path = tmp_path / "project.json"
    ProjectManager.save_project(project, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == CURRENT_PROJECT_VERSION
    assert data["file_info"]["cuts"][0]["label"] == "OP"


def test_load_missing_file_raises_project_error(tmp_path):
    with pytest.raises(ProjectError) as exc_info:
        ProjectManager.load_project(tmp_path / "does_not_exist.json")
    assert exc_info.value.code == "E401"


def test_load_corrupt_json_raises_project_error(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(ProjectError) as exc_info:
        ProjectManager.load_project(path)
    assert exc_info.value.code == "E401"


def test_load_unknown_version_raises_e403(tmp_path):
    project = _sample_project()
    path = tmp_path / "project.json"
    ProjectManager.save_project(project, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = "999.0"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ProjectError) as exc_info:
        ProjectManager.load_project(path)
    assert exc_info.value.code == "E403"


def test_load_missing_required_field_raises_e401(tmp_path):
    project = _sample_project()
    path = tmp_path / "project.json"
    ProjectManager.save_project(project, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    del data["file_info"]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ProjectError) as exc_info:
        ProjectManager.load_project(path)
    assert exc_info.value.code == "E401"


def test_version_8_0_cuts_are_converted_to_keep_ranges(tmp_path):
    # 8.0のcutsは削除する区間。8.1からは残す区間なので補集合になる (2026-10-06)
    data = _sample_project().to_dict()
    data["version"] = "8.0"
    data["file_info"]["cuts"] = [
        {"start_frame": 0, "end_frame": 1000, "label": None},
        {"start_frame": 20000, "end_frame": 25000, "label": "CM"},
    ]
    path = tmp_path / "old.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = ProjectManager.load_project(path)

    assert loaded.version == CURRENT_PROJECT_VERSION
    assert loaded.file_info.cuts == [CutRange(1000, 20000), CutRange(25000, 54000)]


def test_version_8_1_has_no_history_or_excluded_ranges(tmp_path):
    data = _sample_project().to_dict()
    data["version"] = "8.1"
    data.pop("edit_events", None)
    data["file_info"]["cuts"][0].pop("is_excluded", None)
    path = tmp_path / "old_8_1.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = ProjectManager.load_project(path)

    assert loaded.version == CURRENT_PROJECT_VERSION
    assert loaded.edit_events == []
    assert loaded.file_info.cuts[0].is_excluded is False


def test_edit_history_and_excluded_ranges_round_trip(tmp_path):
    project = _sample_project()
    project.file_info.cuts[0].is_excluded = True
    project.edit_events = [{"event_order": 0, "event_type": "toggle_excluded", "details": {"is_excluded": True}}]
    path = tmp_path / "history.json"

    ProjectManager.save_project(project, path)
    loaded = ProjectManager.load_project(path)

    assert loaded.file_info.cuts[0].is_excluded is True
    assert loaded.edit_events == project.edit_events
