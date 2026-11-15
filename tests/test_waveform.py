"""音声波形のピーク値の列(core/waveform.py)。実ffmpegを使う。"""

import subprocess

import numpy as np
import pytest

from ts_smartcut.core.waveform import ENVELOPE_RATE, generate_envelope
from ts_smartcut.errors import FFmpegError


def test_envelope_covers_the_audio_at_10ms_steps(sample_video):
    # サンプル動画は3秒、1000Hzのサイン波
    envelope = generate_envelope(sample_video)
    assert envelope.rate == ENVELOPE_RATE
    assert envelope.peaks.typecode == "h"  # int16
    assert abs(len(envelope.peaks) / envelope.rate - 3.0) < 0.1
    assert np.max(envelope.peaks) > 0


def test_silence_and_sound_land_at_their_times_from_the_video_start(tmp_path):
    # 映像より0.5秒先に音声が始まるTS(放送TSと同じ)。音声の1.5〜2.0秒目だけ音がある
    # = 映像の先頭から1.0〜1.5秒
    path = tmp_path / "offset.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=black:s=160x90:r=30000/1001:d=3",
         "-f", "lavfi", "-i", "sine=f=1000:d=3.5,volume='between(t,1.5,2.0)':eval=frame",
         "-c:v", "mpeg2video", "-c:a", "mp2", "-output_ts_offset", "1",
         "-filter_complex", "[0:v]setpts=PTS+0.5/TB[v]", "-map", "[v]", "-map", "1:a",
         str(path)],
        check=True,
    )
    envelope = generate_envelope(path)
    assert envelope.offset == pytest.approx(-0.5, abs=0.05)

    def peak_at(seconds):  # 映像の先頭からseconds秒
        return int(envelope.peaks[envelope.index_at(seconds)])

    assert peak_at(0.5) == 0 and peak_at(0.95) == 0 and peak_at(1.6) == 0
    assert peak_at(1.05) > 1000 and peak_at(1.45) > 1000


def test_file_without_audio_gives_empty_envelope(tmp_path):
    path = tmp_path / "video_only.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=160x90:d=1",
                    "-c:v", "mpeg2video", str(path)], check=True)
    assert len(generate_envelope(path).peaks) == 0


def test_missing_file_raises(tmp_path):
    with pytest.raises(FFmpegError):
        generate_envelope(tmp_path / "does_not_exist.ts")
