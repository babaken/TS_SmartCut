import pytest

from ts_smartcut.time_utils import (
    format_time,
    frame_to_display,
    frame_to_time,
    frame_to_timestring,
    parse_time,
    time_to_frame,
    timestring_to_frame,
)

FPS_VALUES = [29.97, 30, 25, 60, 23.976, 24]


@pytest.mark.parametrize("fps", FPS_VALUES)
def test_frame_time_round_trip(fps):
    # Sample across ~24h of frames at each fps.
    max_frame = int(fps * 3600 * 24)
    for frame in range(0, max_frame, 997):
        seconds = frame_to_time(frame, fps)
        assert time_to_frame(seconds, fps) == frame


@pytest.mark.parametrize("fps", FPS_VALUES)
def test_frame_timestring_round_trip(fps):
    for frame in (0, 1, 100, 18642, 54000):
        ts = frame_to_timestring(frame, fps)
        assert timestring_to_frame(ts, fps) == frame


def test_format_time_basic():
    assert format_time(0) == "00:00:00.000"
    assert format_time(623.417) == "00:10:23.417"
    assert format_time(3661.5) == "01:01:01.500"


def test_format_time_negative_raises():
    with pytest.raises(ValueError):
        format_time(-1)


def test_parse_time_basic():
    assert parse_time("00:10:23.417") == pytest.approx(623.417)
    assert parse_time("01:01:01.5") == pytest.approx(3661.5)


def test_parse_time_invalid_raises():
    with pytest.raises(ValueError):
        parse_time("not a time")


def test_frame_to_display():
    expected = f"{frame_to_timestring(18642, 29.97)} (Frame: 18642 / 54000)"
    assert frame_to_display(18642, 54000, 29.97) == expected
