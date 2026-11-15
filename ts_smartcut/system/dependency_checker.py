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
