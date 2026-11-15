"""音声波形 (design doc §21)。プレビューの下に、今の位置の前後を表示する。

CMの境目(無音)をフレーム単位で見つけるため、録画全体の音声を細かい区間
(既定10ms)ごとのピーク値にしておき、表示側は今の位置の前後だけを描く。
時刻はカット処理と同じく「映像の先頭が0」に揃える(放送TSは音声が映像より
先に始まる)。
"""

from __future__ import annotations

import json
import math
import subprocess
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from ts_smartcut.errors import FFmpegError
from ts_smartcut.system.child_processes import StalledProcessError, no_window, run_until_stalled

# この秒数の間 ffmpeg が何も出力しなければ止める(ffmpeg/wrapper.STALL_SECONDS と同じ)
STALL_SECONDS = 60.0

SAMPLE_RATE = 8000
ENVELOPE_RATE = 100  # 1秒あたりの点数(10ms刻み)


@dataclass
class AudioEnvelope:
    """音声のピーク値の列。peaks[i]は「映像の先頭からoffset + i/rate秒」から
    1/rate秒間の絶対値の最大(int16の目盛り)。"""

    peaks: array  # int16 (array('h'))
    rate: int
    offset: float  # 最初の点の時刻(映像の先頭基準、秒)。音声が先に始まると負

    def index_at(self, seconds: float) -> int:
        """映像の先頭からseconds秒の位置にあたる点の番号(範囲外もそのまま返す)。"""
        return int(math.floor((seconds - self.offset) * self.rate))


def _stream_starts(file_path: Path, ffprobe_path: Union[str, Path]) -> tuple[float, float, bool]:
    """(ファイル全体のstart_time, 映像のstart_time, 音声があるか)。"""
    result = subprocess.run(
        [str(ffprobe_path), "-v", "quiet", "-show_entries", "format=start_time:stream=codec_type,start_time",
         "-of", "json", str(file_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **no_window(),
    )
    if result.returncode != 0:
        raise FFmpegError("E101", key="exceptions.waveform_probe_failed", path=file_path)
    data = json.loads(result.stdout or "{}")

    def _float(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    video_start = _float(video.get("start_time"))
    format_start = _float(data.get("format", {}).get("start_time")) if data.get("format") else video_start
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return format_start, video_start, has_audio


def generate_envelope(
    file_path: Path,
    ffmpeg_path: Union[str, Path] = "ffmpeg",
    ffprobe_path: Union[str, Path] = "ffprobe",
    rate: int = ENVELOPE_RATE,
) -> AudioEnvelope:
    """録画全体の音声のピーク値の列を作る。音声が無ければ空の列。

    ffmpegは入力の時刻をファイル全体の先頭(多くは音声)が0になるようずらす。
    aresample=async=1:first_pts=0 で、その0から無音で埋めて始め、途中の
    途切れも無音で埋めるので、サンプルの位置がそのまま時刻になる。"""
    file_path = Path(file_path)
    format_start, video_start, has_audio = _stream_starts(file_path, ffprobe_path)
    if not has_audio:
        return AudioEnvelope(peaks=array("h"), rate=rate, offset=0.0)
    cmd = [
        str(ffmpeg_path), "-v", "error", "-i", str(file_path),
        "-map", "0:a:0", "-ac", "1", "-af", f"aresample={SAMPLE_RATE}:async=1:first_pts=0",
        "-f", "s16le", "-",
    ]
    try:
        result = run_until_stalled(cmd, STALL_SECONDS)
    except StalledProcessError as e:
        raise FFmpegError("E101", key="exceptions.waveform_stalled", seconds=f"{e.seconds:.0f}", path=file_path) from e
    if result.returncode != 0:
        raise FFmpegError("E101", key="exceptions.waveform_failed",
                          error=result.stderr.decode("utf-8", errors="replace"))

    return AudioEnvelope(peaks=peaks_from_samples(result.stdout, rate), rate=rate, offset=format_start - video_start)


def peaks_from_samples(samples, rate: int = ENVELOPE_RATE) -> array:
    """SAMPLE_RATE のモノラル int16 の音声 (バイト列、または int16 の並び) を、1/rate 秒ごとの絶対値の最大
    (array('h')) にする。"""
    from ts_smartcut.autocut.native import native_module

    data = bytes(samples) if isinstance(samples, (bytes, bytearray)) else memoryview(samples).cast("B").tobytes()
    data = data[:len(data) // 2 * 2]
    peaks = array("h")
    peaks.frombytes(native_module().audio_peaks(data, rate))
    return peaks
