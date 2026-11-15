"""録画の特徴(autocut/features.py)を計算し、時間・大きさ・中身の要約を表示する(自動カット A3)。

使い方:
    python tools/check_features.py <録画.ts> [...]

DB には書かない(確認用)。残した区間は分からないので、四隅は全部「それ以外」として集計する。
音量は既存の音声波形(core/waveform.py の generate_envelope、ファイルを ffmpeg が直接読む)と
比べ、一致した点の割合と、無音(-60dB 未満)とみなす点の数の違いを出す。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_smartcut.autocut.features import compute_features, peaks_to_level  # noqa: E402
from ts_smartcut.broadcast.program_info import read_program_info  # noqa: E402
from ts_smartcut.core.waveform import generate_envelope  # noqa: E402
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper  # noqa: E402


def check(path: Path) -> None:
    info = read_program_info(path)
    fps = FFmpegWrapper().get_file_info(path).fps
    started = time.monotonic()
    f = compute_features(path, fps, [], service_id=info.service_id if info else None)
    elapsed = time.monotonic() - started
    blob = f.to_blob()
    print(f"== {path.name}")
    print(f"  時間 {elapsed:.1f} 秒、大きさ {len(blob) / 1024:.0f} KB、最後まで {f.complete}")
    print(f"  フレーム {f.frame_count}、縮小 {f.decoded_size}、四隅 {f.corner_size}")
    scene = np.asarray(f.scene_change, np.float32)
    print(f"  場面の切り替わり(差 > 30): {int((scene > 30).sum())} 箇所")
    db = np.asarray(f.audio_db())
    print(f"  音量 {len(db)} 点、無音(-60dB 未満) {int((db < -60).sum())} 点")
    print(f"  音声のチャンネル数の変化: {[(round(t, 2), c) for t, c in f.streams.audio_channels]}")
    print(f"  PMT の変化: {len(f.streams.pmt_changes)} 回、字幕文 {len(f.streams.caption_times)} 回")

    ref = np.asarray(peaks_to_level(generate_envelope(path).peaks)).astype(int)
    a = np.asarray(f.audio_level).astype(int)
    n = min(len(a), len(ref))
    q = (-60 + 100) * 2
    print(f"  音声波形との比較: 点数 {len(a)} / {len(ref)}、一致 {np.mean(a[:n] == ref[:n]):.1%}、"
          f"差の最大 {np.abs(a[:n] - ref[:n]).max() / 2:.1f}dB、無音の判定の違い {int(((a[:n] < q) != (ref[:n] < q)).sum())} 点")


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        check(Path(arg))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
