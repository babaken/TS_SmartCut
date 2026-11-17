import json
from datetime import datetime
from pathlib import Path

from ts_smartcut.models import (
    AppSettings,
    BatchItem,
    Bookmark,
    CutRange,
    FileCutInfo,
    OperationHistory,
    ProcessProgress,
    Project,
)


def _sample_file_info() -> FileCutInfo:
    return FileCutInfo(
        file_path=Path("/videos/sample.mp4"),
        total_frames=54000,
        fps=29.97,
        resolution=(1920, 1080),
        video_codec="h264",
        audio_codec="aac",
        bitrate=5_000_000,
        duration=1801.8,
        file_size=1_234_567_890,
        cuts=[CutRange(1000, 5000, "OP"), CutRange(10000, 15000, None)],
        bookmarks=[Bookmark(3000, "CM開始", datetime(2026, 2, 12, 10, 35, 0))],
        keyframes=[0, 250, 500],
    )


def _roundtrip_via_json(obj):
    cls = type(obj)
    return cls.from_dict(json.loads(json.dumps(obj.to_dict())))


def test_cut_range_round_trip():
    cut = CutRange(1000, 5000, "OP")
    assert _roundtrip_via_json(cut) == cut


def test_bookmark_round_trip():
    bm = Bookmark(3000, "CM開始", datetime(2026, 2, 12, 10, 35, 0))
    assert _roundtrip_via_json(bm) == bm


def test_file_cut_info_round_trip():
    info = _sample_file_info()
    restored = _roundtrip_via_json(info)
    assert restored == info
    assert isinstance(restored.file_path, Path)
    assert isinstance(restored.resolution, tuple)



def test_file_cut_info_vfr_flag_round_trips_and_defaults_to_false():
    import dataclasses
    info = dataclasses.replace(_sample_file_info(), is_vfr=True)
    assert _roundtrip_via_json(info).is_vfr is True
    data = _sample_file_info().to_dict()
    del data["is_vfr"]  # is_vfr追加前に保存したプロジェクト
    assert FileCutInfo.from_dict(data).is_vfr is False

def test_batch_item_round_trip():
    item = BatchItem(
        order=0,
        file_info=_sample_file_info(),
        status="Idle",
        output_path=Path("/out/sample_cut.mp4"),
    )
    restored = _roundtrip_via_json(item)
    assert restored == item
    assert isinstance(restored.output_path, Path)


def test_process_progress_round_trip():
    progress = ProcessProgress(
        current_file=1,
        total_files=3,
        current_phase="Body",
        progress_percent=42.5,
        elapsed_time=12.3,
        estimated_remaining=17.0,
    )
    assert _roundtrip_via_json(progress) == progress


def test_app_settings_round_trip_with_paths():
    settings = AppSettings(
        temp_directory=Path("/tmp/ts_smartcut"),
        ffmpeg_path=Path("/usr/bin/ffmpeg"),
        default_output_dir=None,
        cpu_threads=4,
    )
    restored = _roundtrip_via_json(settings)
    assert restored == settings
    assert isinstance(restored.temp_directory, Path)
    assert restored.default_output_dir is None


def test_app_settings_defaults_round_trip():
    settings = AppSettings()
    assert _roundtrip_via_json(settings) == settings


def test_project_round_trip():
    project = Project(
        version="8.0",
        file_info=_sample_file_info(),
        settings=AppSettings(),
        created_at=datetime(2026, 2, 12, 10, 30, 0),
        modified_at=datetime(2026, 2, 12, 11, 45, 0),
    )
    restored = _roundtrip_via_json(project)
    assert restored == project


def test_operation_history_round_trip_with_json_safe_data():
    op = OperationHistory(
        operation_type="add_cut",
        data={"start_frame": 1000, "end_frame": 5000},
        timestamp=datetime(2026, 2, 12, 10, 30, 0),
    )
    assert _roundtrip_via_json(op) == op


def test_removed_ranges_is_the_complement_of_the_keep_ranges():
    from ts_smartcut.models import removed_ranges

    assert removed_ranges(100, []) == [CutRange(0, 100)]
    assert removed_ranges(100, [CutRange(0, 100)]) == []
    assert removed_ranges(100, [CutRange(60, 90), CutRange(10, 40)]) == [
        CutRange(0, 10), CutRange(40, 60), CutRange(90, 100)
    ]
    # 重なり・範囲外はまとめて丸める
    assert removed_ranges(100, [CutRange(10, 50), CutRange(30, 60), CutRange(90, 150)]) == [
        CutRange(0, 10), CutRange(60, 90)
    ]


def test_empty_setting_paths_mean_not_set():
    # 空の文字列は Path(".")(今のフォルダ)になり、そこへ出力していた(2026-11-17)
    settings = AppSettings.from_dict({"default_output_dir": "", "temp_directory": "  ", "ffmpeg_path": ""})
    assert settings.default_output_dir is None
    assert settings.temp_directory is None
    assert settings.ffmpeg_path is None
