"""External dependency checks (design doc §12): ffmpeg/ffprobe/mpv presence
and version, with support for user-configured executable paths
(AppSettings.ffmpeg_path / ffprobe_path / mpv_path)."""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Union

from ts_smartcut.ffmpeg.compat import MIN_FFMPEG_MAJOR
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

# "n8.1.3-35-g…"(BtbN)、"9.0.2-essentials_build-…"(gyan.dev)、"7:6.1.1-3ubuntu5"(Debian の epoch 付き)など
_FFMPEG_MAJOR = re.compile(r"^(?:\d+:)?n?(\d+)\.\d")
_FFMPEG_CONFIGURATION = re.compile(r"^configuration:.*$", re.MULTILINE)

# `ffmpeg -version` の出力(ffmpeg の場所ごと、読めた時だけ)。起動時の警告と GPU の判定が使う
_ffmpeg_outputs: Dict[str, str] = {}
_ffmpeg_outputs_lock = threading.Lock()
# 最初の起動はウイルス対策の検査などで遅いことがあるので、ほかの版の確かめ(5 秒)より長く待つ
_FFMPEG_VERSION_TIMEOUT = 20


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
    def ffmpeg_version_output(ffmpeg_path: Optional[Union[str, Path]] = None) -> Optional[str]:
        """`ffmpeg -version` の出力(見つからない・読めない時は None)。版を読む所はここだけ。

        ffmpeg の場所ごとに一度だけ動かし、読めた時だけ覚える。失敗も覚えていた頃は、遅い最初の起動で
        時間切れになると、アプリを閉じるまで版が分からないままだった(2026-11-17)。"""
        key = str(ffmpeg_path or "ffmpeg")
        with _ffmpeg_outputs_lock:
            if key in _ffmpeg_outputs:
                return _ffmpeg_outputs[key]
        output = DependencyChecker._run_version(key, _VERSION_ARGS["ffmpeg"], timeout=_FFMPEG_VERSION_TIMEOUT)
        if output is not None and _VERSION_PATTERNS["ffmpeg"].search(output):
            with _ffmpeg_outputs_lock:
                _ffmpeg_outputs[key] = output
        return output

    @staticmethod
    def clear_ffmpeg_version_cache() -> None:
        """覚えた `ffmpeg -version` の出力を捨てる(テスト用)。"""
        with _ffmpeg_outputs_lock:
            _ffmpeg_outputs.clear()

    @staticmethod
    def ffmpeg_version(ffmpeg_path: Optional[Union[str, Path]] = None) -> Optional[str]:
        """ffmpeg の版の文字("n8.1.3-35-g…" など。見つからない・読めない時は None)。"""
        match = _VERSION_PATTERNS["ffmpeg"].search(DependencyChecker.ffmpeg_version_output(ffmpeg_path) or "")
        return match.group(1) if match else None

    @staticmethod
    def ffmpeg_build_id(ffmpeg_path: Optional[Union[str, Path]] = None) -> Optional[str]:
        """ffmpeg のビルドを表す文字: 版と、ビルドの構成(configuration の行)の短いハッシュ。
        同じ版でも配布元が違えば入っている部品(GPU の部品など)が違うので、両方で見分ける。
        GPU の判定に控え、変わったら判定し直す(2026-11-17、利用者の決定)。"""
        # 版も同じ出力から読む(ffmpeg_version を呼ぶと、読めない時にもう一度 ffmpeg を動かして待つ)
        output = DependencyChecker.ffmpeg_version_output(ffmpeg_path)
        match = _VERSION_PATTERNS["ffmpeg"].search(output or "")
        if output is None or match is None:
            return None
        version = match.group(1)
        configuration = _FFMPEG_CONFIGURATION.search(output)
        digest = hashlib.sha1((configuration.group(0) if configuration else "").encode("utf-8")).hexdigest()[:12]
        return f"{version} ({digest})"

    @staticmethod
    def old_ffmpeg_version(ffmpeg_path: Optional[Union[str, Path]] = None) -> Optional[str]:
        """ffmpeg が MIN_FFMPEG_MAJOR より古ければその版を返す(新しい・見つからない・読めない時は None)。"""
        version = DependencyChecker.ffmpeg_version(ffmpeg_path)
        major = ffmpeg_major_version(version) if version is not None else None
        return version if major is not None and major < MIN_FFMPEG_MAJOR else None
