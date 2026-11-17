"""ffmpeg の版による違いの手当て(compat.py)。"""

from pathlib import Path

import pytest

from ts_smartcut.ffmpeg import compat
from ts_smartcut.ffmpeg.compat import (
    NO_FRAMES,
    bytes_per_frame,
    progress_frames,
    x264_rate_args,
)


@pytest.mark.parametrize("line, expected", [
    ("frame=  120 fps=0.0 q=-1.0 size=    1024kB time=00:00:00.00 bitrate=N/A speed=N/A\n", 120),
    ("size=    1000kB time=00:00:00.00 bitrate=N/A speed=N/A\n", 16),  # 6.x のコピー
    ("Lsize=    2000KiB time=00:00:00.00 bitrate=N/A speed=N/A\n", 32),
    ("Input #0, mpegts, from 'a.ts':\n", None),
    ("  Stream #0:0[0x111]: Video: mpeg2video\n", None),
])
def test_progress_frames(line, expected):
    assert progress_frames(line, 64000.0) == expected


def test_progress_frames_size_without_estimate():
    assert progress_frames("size=    1000kB time=00:00:00.00\n") == NO_FRAMES


def test_bytes_per_frame():
    assert bytes_per_frame(16_000_000, 29.97) == pytest.approx(16_000_000 / 8 / 29.97)
    assert bytes_per_frame(0, 29.97) is None
    assert bytes_per_frame(16_000_000, 0) is None


def test_x264_rate_args():
    assert x264_rate_args(30000 / 1001) == ["-x264-params", "fps=30000/1001"]
    assert x264_rate_args(60000 / 1001) == ["-x264-params", "fps=60000/1001"]


def test_version_handling_is_only_in_compat():
    """版による違いの手当ては compat.py に集める(ほかの所に散らばらないように)。"""
    root = Path(compat.__file__).resolve().parents[1]
    markers = ("-x264-params", "size=\\s*", "MIN_FFMPEG_MAJOR =")
    found = [
        f"{path.relative_to(root)}: {marker}"
        for path in root.rglob("*.py") if path.name != "compat.py"
        for marker in markers if marker in path.read_text(encoding="utf-8")
    ]
    assert found == []
