from pathlib import Path

import pytest

from ts_smartcut.errors import DiskSpaceError
from ts_smartcut.models import CutRange, FileCutInfo
from ts_smartcut.system.disk_space_manager import SAFETY_MARGIN, DiskSpaceManager


def _file_info(file_size: int) -> FileCutInfo:
    return FileCutInfo(
        file_path=Path("/videos/sample.mp4"),
        total_frames=54000,
        fps=29.97,
        resolution=(1920, 1080),
        video_codec="h264",
        audio_codec="aac",
        bitrate=5_000_000,
        duration=1801.8,
        file_size=file_size,
    )


def test_check_available_space_returns_positive_int(tmp_path):
    available = DiskSpaceManager.check_available_space(tmp_path)
    assert isinstance(available, int)
    assert available > 0


def test_estimate_required_space_scales_with_kept_ratio():
    info = _file_info(1_000_000_000)  # total_frames=54000
    # 4分の1をカット -> 残り4分の3
    required = DiskSpaceManager.estimate_required_space(info, [CutRange(0, 13500)])
    assert required == int(1_000_000_000 * 0.75 * SAFETY_MARGIN)


def test_estimate_required_space_merges_overlapping_cuts():
    info = _file_info(1_000_000_000)
    required = DiskSpaceManager.estimate_required_space(
        info, [CutRange(0, 13500), CutRange(10000, 27000)]
    )
    assert required == int(1_000_000_000 * 0.5 * SAFETY_MARGIN)


def test_check_before_processing_passes_when_space_available(tmp_path):
    info = _file_info(1024)  # tiny file, plenty of real disk space
    assert DiskSpaceManager.check_before_processing(info, [], tmp_path) is True


def test_check_before_processing_raises_when_space_insufficient(tmp_path):
    available = DiskSpaceManager.check_available_space(tmp_path)
    huge_file_size = int(available / SAFETY_MARGIN) + 1
    info = _file_info(huge_file_size)

    with pytest.raises(DiskSpaceError) as exc_info:
        DiskSpaceManager.check_before_processing(info, [], tmp_path)

    assert exc_info.value.code == "E004"


def test_same_filesystem_requires_room_for_output_and_temp_together(tmp_path):
    # 出力1本分は入るが、一時ファイルと合わせた2本分は入らない
    available = DiskSpaceManager.check_available_space(tmp_path)
    one_copy = int(available * 0.6 / SAFETY_MARGIN)
    info = _file_info(one_copy)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()

    with pytest.raises(DiskSpaceError):
        DiskSpaceManager.check_before_processing(info, [], tmp_path, temp_dir)


def test_temp_on_another_drive_needs_room_for_parts_and_the_joined_output(tmp_path, monkeypatch):
    # 出力は一時フォルダで結合してから出力先へコピーする(2026-10-22)。一時フォルダには部品と
    # 結合した出力の2本分、出力先には1本分が要る
    output_dir, temp_dir = tmp_path / "out", tmp_path / "temp"
    output_dir.mkdir()
    temp_dir.mkdir()
    info = _file_info(1_000_000)
    need = DiskSpaceManager.estimate_required_space(info, [])
    free = {output_dir: need, temp_dir: need * 2}
    monkeypatch.setattr(DiskSpaceManager, "_same_filesystem", staticmethod(lambda a, b: False))
    monkeypatch.setattr(DiskSpaceManager, "check_available_space", staticmethod(lambda path: free[path]))

    assert DiskSpaceManager.check_before_processing(info, [], output_dir, temp_dir) is True
    free[temp_dir] = need * 2 - 1
    with pytest.raises(DiskSpaceError):
        DiskSpaceManager.check_before_processing(info, [], output_dir, temp_dir)
