"""External dependency checks (design doc §12): ffmpeg/ffprobe/mpv presence
and version, with support for user-configured executable paths
(AppSettings.ffmpeg_path / ffprobe_path / mpv_path)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Union

from ts_smartcut.system.child_processes import no_window

_VERSION_ARGS: Dict[str, List[str]] = {
    "ffmpeg": ["-version"],
    "ffprobe": ["-version"],
    "mpv": ["--version"],
}

_VERSION_PATTERNS: Dict[str, re.Pattern] = {
    "ffmpeg": re.compile(r"ffmpeg version (\S+)"),
    "ffprobe": re.compile(r"ffprobe version (\S+)"),
    "mpv": re.compile(r"mpv\s+v?(\S+)"),
}

# 必要な ffmpeg の版(README・資料と同じ)。7.x はインターレースの H.264 をビットレート指定で
# 再エンコードすると灰色の絵になり、6.x はストリームのコピーで進み具合を出さない(2026-11-15)
MIN_FFMPEG_MAJOR = 8
# "n8.1.3-35-g…"(BtbN)、"9.0.2-essentials_build-…"(gyan.dev)、"7:6.1.1-3ubuntu5"(Debian の epoch 付き)など
_FFMPEG_MAJOR = re.compile(r"^(?:\d+:)?n?(\d+)\.\d")


def ffmpeg_major_version(version: str) -> Optional[int]:
    """get_versions() の ffmpeg の版から主の番号を取る。開発版("N-12345-g…")など読めない時は None。"""
    match = _FFMPEG_MAJOR.match(version)
    return int(match.group(1)) if match else None


class DependencyChecker:
    @staticmethod
    def _run_version(exe: Union[str, Path], args: List[str], timeout: float = 5) -> Optional[str]:
        try:
            result = subprocess.run(
                [str(exe), *args],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                **no_window(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    @staticmethod
    def check_ffmpeg(exe: Optional[Union[str, Path]] = None) -> bool:
        return DependencyChecker._run_version(exe or "ffmpeg", _VERSION_ARGS["ffmpeg"]) is not None

    @staticmethod
    def check_ffprobe(exe: Optional[Union[str, Path]] = None) -> bool:
        return DependencyChecker._run_version(exe or "ffprobe", _VERSION_ARGS["ffprobe"]) is not None

    @staticmethod
    def check_mpv(exe: Optional[Union[str, Path]] = None) -> bool:
        return DependencyChecker._run_version(exe or "mpv", _VERSION_ARGS["mpv"]) is not None

    @staticmethod
    def check_all(
        ffmpeg_path: Optional[Union[str, Path]] = None,
        ffprobe_path: Optional[Union[str, Path]] = None,
        mpv_path: Optional[Union[str, Path]] = None,
    ) -> Dict[str, bool]:
        return {
            "ffmpeg": DependencyChecker.check_ffmpeg(ffmpeg_path),
            "ffprobe": DependencyChecker.check_ffprobe(ffprobe_path),
            "mpv": DependencyChecker.check_mpv(mpv_path),
        }

    @staticmethod
    def get_versions(
        ffmpeg_path: Optional[Union[str, Path]] = None,
        ffprobe_path: Optional[Union[str, Path]] = None,
        mpv_path: Optional[Union[str, Path]] = None,
    ) -> Dict[str, str]:
        versions: Dict[str, str] = {}
        for tool, exe in (
            ("ffmpeg", ffmpeg_path or "ffmpeg"),
            ("ffprobe", ffprobe_path or "ffprobe"),
            ("mpv", mpv_path or "mpv"),
        ):
            output = DependencyChecker._run_version(exe, _VERSION_ARGS[tool])
            if output is None:
                versions[tool] = "Not found"
                continue
            match = _VERSION_PATTERNS[tool].search(output)
            versions[tool] = match.group(1) if match else "Unknown"
        return versions

    @staticmethod
    def old_ffmpeg_version(ffmpeg_path: Optional[Union[str, Path]] = None) -> Optional[str]:
        """ffmpeg が MIN_FFMPEG_MAJOR より古ければその版を返す(新しい・見つからない・読めない時は None)。"""
        output = DependencyChecker._run_version(ffmpeg_path or "ffmpeg", _VERSION_ARGS["ffmpeg"])
        match = _VERSION_PATTERNS["ffmpeg"].search(output or "")
        if not match:
            return None
        major = ffmpeg_major_version(match.group(1))
        return match.group(1) if major is not None and major < MIN_FFMPEG_MAJOR else None
