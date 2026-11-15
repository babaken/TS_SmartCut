"""音声の形式・字幕の変化 (設計書 §31.3、自動カット計画 A3)。

CM では音声の形式(ステレオ／5.1ch／二か国語)や字幕の有無が切り替わることが
ある。録画の TS を先頭から最後まで読み、次の位置を集める。

- PMT の変化: 録画した番組(サービス)の ES の構成(音声・字幕の ES の増減)
- 音声のチャンネル数: 最初の音声 ES (AAC の ADTS ヘッダの channel_configuration)
  が変わった位置。0 は ADTS の外で指定する形式(二か国語のデュアルモノ等)
- 字幕: 字幕の ES (component_tag 0x30〜0x37) で、字幕の本文(字幕文の
  データグループ)が送られた時刻。字幕の管理データは字幕が無くても数秒ごとに
  送られるので数えない

時刻は特徴の他の値と同じく映像の先頭が 0 (秒)。PES の PTS があればそれを使い、
無いもの(PMT 等)は直前の映像の PTS を使う(0.1 秒程度ずれる)。

解析は Rust (native/src/scanner.rs の StreamScanner)。特徴の計算では、録画を ffmpeg に渡しながら
同じデータを解析する (features.py)。2026-10-28 まで同じ解析を Python でも持っていた。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import Packed

logger = logging.getLogger(__name__)

CHUNK_SIZE = 32 * 1024 * 1024


@dataclass
class StreamEvents:
    # (時刻, [(pid, stream_type, component_tag), ...])。最初の PMT も入る
    pmt_changes: List[Tuple[float, List[Tuple[int, int, Optional[int]]]]] = field(default_factory=list)
    # (時刻, channel_configuration)。最初の値も入る
    audio_channels: List[Tuple[float, int]] = field(default_factory=list)
    caption_times: List[float] = field(default_factory=list)  # 保存は float32 (features.py)
    complete: bool = True

    def __post_init__(self):
        self.caption_times = [float(t) for t in self.caption_times]

    def to_meta(self) -> dict:
        return {
            "pmt_changes": [[t, [list(s) for s in streams]] for t, streams in self.pmt_changes],
            "audio_channels": [[t, c] for t, c in self.audio_channels],
            "complete": self.complete,
        }

    @classmethod
    def from_meta(cls, meta: dict, caption_times: List[float]) -> "StreamEvents":
        return cls(
            pmt_changes=[(t, [tuple(s) for s in streams]) for t, streams in meta.get("pmt_changes", [])],
            audio_channels=[(t, c) for t, c in meta.get("audio_channels", [])],
            caption_times=list(caption_times),
            complete=meta.get("complete", True),
        )

    @classmethod
    def from_native(cls, pmt_changes, audio_channels, captions, complete: bool) -> "StreamEvents":
        """Rust の StreamScanner の結果から。"""
        return cls(
            pmt_changes=[(t, [tuple(s) for s in layout]) for t, layout in pmt_changes],
            audio_channels=[(t, c) for t, c in audio_channels],
            caption_times=Packed.from_values(captions, "f4").flat(),  # float32 に丸める (保存する値と同じ)
            complete=complete,
        )


def scan_stream_events(
    file_path: Path,
    video_start: float,
    service_id: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
) -> StreamEvents:
    """録画全体を読み、音声の形式・字幕の変化を集める(確認用。特徴の計算では、ffmpeg に渡す
    データを features.py が同じ解析に通す)。解析で失敗した所があれば complete=False。"""
    scanner = native_module().StreamScanner(service_id, video_start)
    complete = True
    try:
        with open(file_path, "rb") as f:
            while True:
                if stop_event is not None and stop_event.is_set():
                    complete = False
                    break
                data = f.read(CHUNK_SIZE)
                if not data:
                    break
                error = scanner.feed_bytes(data)
                if error is not None:
                    logger.warning("Could not parse audio/caption changes: %s (%s)", file_path, error)
                    complete = False
    except OSError as e:
        logger.warning("Could not read audio/caption changes: %s (%s)", file_path, e)
        complete = False
    return StreamEvents.from_native(*scanner.result(), complete=complete)
