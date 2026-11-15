"""録画の音声の一覧と、選んだ音声 (設計書 §4.12)。

一覧は先頭の PMT の音声に、ファイルの中ほど(番組の途中)の PMT で加わった音声を足したもの
(GR-B の 2 つ目の音声は番組が始まってから加わる)。順は PMT の順で、0 番が「最初の音声」
(ffmpeg の `0:a:0`。自動カットの案と特徴はいつもこの音声で作る)。名前・種類・言語は番組情報の
音声コンポーネント記述子(EIT 0xC4)から component_tag で対応づける。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional

from ts_smartcut.i18n.language_manager import tr

# 音声の ES の stream_type(native/src/scanner.rs の AUDIO_STREAM_TYPES と同じ)
AUDIO_STREAM_TYPES = {0x0F, 0x11, 0x03, 0x04, 0x81}

# component_type(ARIB STD-B10)→ 種類の文字のキー
_KINDS = {0x01: "audio.kind_mono", 0x02: "audio.kind_dual_mono", 0x03: "audio.kind_stereo", 0x09: "audio.kind_5_1"}
_MULTI = range(0x04, 0x09)  # 2/1・3/0・2/2・3/1・3/2


@dataclass
class AudioTrack:
    index: int  # 一覧の何番目か(0 = 最初の音声)
    pid: int
    component_tag: Optional[int] = None
    component_type: Optional[int] = None
    language: str = ""
    language2: str = ""
    text: str = ""  # 放送局が入れた説明(無ければ "")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AudioTrack":
        return cls(
            index=int(data["index"]), pid=int(data["pid"]), component_tag=data.get("component_tag"),
            component_type=data.get("component_type"), language=data.get("language", ""),
            language2=data.get("language2", ""), text=data.get("text", ""),
        )

    def name(self) -> str:
        """名前(説明。無ければ「音声 N」)。"""
        return self.text or tr("audio.track_number", number=self.index + 1)

    def kind(self) -> str:
        if self.component_type in _KINDS:
            return tr(_KINDS[self.component_type])
        if self.component_type in _MULTI:
            return tr("audio.kind_multi")
        return ""

    def languages(self) -> str:
        names = [_language_name(code) for code in (self.language, self.language2) if code]
        return " / ".join(names)

    def label(self) -> str:
        """「1. 主音声  ステレオ  日本語」の形の 1 行。"""
        parts = [f"{self.index + 1}. {self.name()}", self.kind(), self.languages()]
        return "  ".join(p for p in parts if p)


def _language_name(code: str) -> str:
    key = f"audio.lang_{code}"
    text = tr(key)
    return code if text == key else text


def audio_tracks(info) -> List[AudioTrack]:
    """ProgramInfo から選べる音声の一覧(番組情報が無ければ空)。"""
    if info is None:
        return []
    streams = [s for s in list(info.streams) + list(getattr(info, "programme_streams", []) or [])
               if s.stream_type in AUDIO_STREAM_TYPES]
    seen = set()
    ordered = []
    for s in streams:
        key = s.component_tag if s.component_tag is not None else ("pid", s.pid)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(s)
    event = getattr(info, "event", None)
    components = {c.component_tag: c for c in getattr(event, "audio_components", None) or []}
    tracks = []
    for index, s in enumerate(ordered):
        c = components.get(s.component_tag)
        tracks.append(AudioTrack(
            index=index, pid=s.pid, component_tag=s.component_tag,
            component_type=c.component_type if c else None,
            language=c.language if c else "", language2=c.language2 if c else "", text=c.text if c else "",
        ))
    return tracks


def first_track(tracks: List[AudioTrack]) -> Optional[AudioTrack]:
    return tracks[0] if tracks else None


def same_track(a: Optional[AudioTrack], b: Optional[AudioTrack]) -> bool:
    """同じ音声か(component_tag があればそれで、無ければ PID で比べる)。"""
    if a is None or b is None:
        return a is b
    if a.component_tag is not None and b.component_tag is not None:
        return a.component_tag == b.component_tag
    return a.pid == b.pid
