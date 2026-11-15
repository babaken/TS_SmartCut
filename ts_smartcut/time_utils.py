"""Frame/time conversion (design doc §5).

Frame numbers are the single source of truth internally (CLAUDE.md).
HH:MM:SS.mmm strings are a display-only projection — convert at the
boundary, never store or compare on them.
"""

from __future__ import annotations

import re

_TIME_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def frame_to_time(frame: int, fps: float) -> float:
    """Frame number -> seconds."""
    return frame / fps


def time_to_frame(seconds: float, fps: float) -> int:
    """Seconds -> frame number."""
    return round(seconds * fps)


def format_time(seconds: float) -> str:
    """Seconds -> "HH:MM:SS.mmm" (millisecond precision)."""
    if seconds < 0:
        raise ValueError("seconds must be non-negative")
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def parse_time(time_str: str) -> float:
    """"HH:MM:SS.mmm" -> seconds."""
    match = _TIME_RE.match(time_str.strip())
    if not match:
        raise ValueError(f"Invalid time format: {time_str!r}")
    hours, minutes, secs = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)


def frame_to_timestring(frame: int, fps: float) -> str:
    """Frame number -> "HH:MM:SS.mmm" directly."""
    return format_time(frame_to_time(frame, fps))


def timestring_to_frame(time_str: str, fps: float) -> int:
    """"HH:MM:SS.mmm" -> frame number directly."""
    return time_to_frame(parse_time(time_str), fps)


def frame_to_display(frame: int, total_frames: int, fps: float) -> str:
    """"HH:MM:SS.mmm (Frame: current / total)" per design doc §5.3."""
    return f"{frame_to_timestring(frame, fps)} (Frame: {frame} / {total_frames})"
