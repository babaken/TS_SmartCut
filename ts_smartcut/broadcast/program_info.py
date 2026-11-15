"""録画した TS の番組情報(SI)を読み、録画した番組を選ぶ(設計書 §31.2、自動カット計画 A1)。

読むもの:
- PAT / PMT: サービス(service_id)、PCR の PID、映像・音声・字幕の構成
- SDT: 局名、事業者名、original_network_id(地上波・BS・CS の区別)
- EIT(現在・次の番組、table_id 0x4E): 番組名、放送開始時刻、長さ、ジャンル、説明・詳細
- TOT / TDT: 放送の現在時刻 → 録画開始・終了の時刻

TS を読んでセクションを解析し、ARIB の文字を戻すのは Rust の部品(native/src/program_info.rs・arib.rs・
ts.rs。2026-10-28 までは Python の ts_sections.py・arib_text.py とこのファイル)。ここでは、その結果から
録画した番組を選び、録画の時刻を組み立てる。

番組情報の解析は rplstool-cs(RplsTool.Core/TsProgramInfoReader.cs)の移植。元は
rplsinfo(https://github.com/tsukumijima/rplsinfo。作者の記載により改変・再配布は自由)。

元との違い:
- 元はファイルの中ほど(50%の位置)の EIT の「現在の番組」だけを読む。こちらは先頭・中ほど・
  末尾の EIT の現在・次の番組を集め、TOT から求めた録画の時間帯と重なりが最も大きい番組を
  選ぶ。録画マージンのため、先頭の EIT は前の番組を指していることがある(CS-A・BS-A の
  録画で確認、2026-10-07)。
- 録画開始・終了の時刻を、最初・最後の TOT と、その位置までの PCR の進みから求める。
- 改行は "\\n"(元は "\\r\\n")。
- SIT(rpls や部分 TS の形式)は読まない。対象は放送の録画だけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ts_smartcut.autocut.native import native_module

JST = timezone(timedelta(hours=9), "JST")


@dataclass
class StreamInfo:
    pid: int
    stream_type: int
    component_tag: Optional[int] = None


@dataclass
class AudioComponent:
    """EIT の音声コンポーネント記述子(0xC4)。番組の音声 1 つ分(設計書 §4.12)。"""

    component_tag: int
    component_type: int  # 0x01 モノラル・0x02 二か国語(デュアルモノ)・0x03 ステレオ・0x09 5.1ch 等
    stream_type: int
    main: bool  # main_component_flag(主音声)
    language: str = ""  # ISO 639 の 3 文字("jpn" 等)
    language2: str = ""  # 二か国語の 2 つ目の言語(無ければ "")
    text: str = ""  # 放送局が入れた説明(「主音声」「解説」等。無いことも多い)


@dataclass
class EventInfo:
    event_id: int
    start: Optional[datetime]  # 放送開始時刻(日本時間)
    duration: Optional[timedelta]
    name: str = ""
    description: str = ""  # 短形式イベント記述子の説明(CLI の Detail)
    extended: str = ""  # 拡張形式イベント記述子(CLI の Extend)
    genres: List[int] = field(default_factory=list)  # content_nibble_level_1 << 4 | level_2
    audio_components: List[AudioComponent] = field(default_factory=list)  # 0xC4、放送順

    @property
    def end(self) -> Optional[datetime]:
        if self.start is None or self.duration is None:
            return None
        return self.start + self.duration


@dataclass
class ProgramInfo:
    service_id: int
    original_network_id: Optional[int] = None
    transport_stream_id: Optional[int] = None
    service_name: str = ""
    provider_name: str = ""
    recording_start: Optional[datetime] = None  # 日本時間
    recording_end: Optional[datetime] = None
    event: Optional[EventInfo] = None  # 録画した番組
    events: List[EventInfo] = field(default_factory=list)  # 録画の中で見つかった番組(放送順)
    streams: List[StreamInfo] = field(default_factory=list)  # 先頭の PMT
    # ファイルの中ほど(番組の途中)の PMT。番組が始まってから加わる音声(GR-B の 2 つ目)はこちらにだけある
    programme_streams: List[StreamInfo] = field(default_factory=list)
    pcr_pid: Optional[int] = None
    packet_size: int = 188

    @property
    def broadcast_type(self) -> str:
        """"terrestrial" / "bs" / "cs" / "unknown"(original_network_id による)。"""
        return broadcast_type(self.original_network_id)


def broadcast_type(original_network_id: Optional[int]) -> str:
    if original_network_id is None:
        return "unknown"
    if original_network_id == 0x0004:
        return "bs"
    if original_network_id in (0x0006, 0x0007):
        return "cs"
    if 0x7880 <= original_network_id <= 0x7FE8:
        return "terrestrial"
    return "unknown"


# ---- Rust の結果を日時・データクラスにする -------------------------------------

def mjd_to_date(mjd: int) -> date:
    return date(1858, 11, 17) + timedelta(days=mjd)


def _time(raw: Optional[Tuple[int, int, int, int]]) -> Optional[datetime]:
    """(MJD, 時, 分, 秒)。時・分・秒は BCD を戻した値(範囲の外もそのまま足す)。"""
    if raw is None:
        return None
    mjd, hours, minutes, seconds = raw
    d = mjd_to_date(mjd)
    return datetime(d.year, d.month, d.day, tzinfo=JST) + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _duration(raw: Optional[Tuple[int, int, int]]) -> Optional[timedelta]:
    if raw is None:
        return None
    return timedelta(hours=raw[0], minutes=raw[1], seconds=raw[2])


def _streams(raw) -> List[StreamInfo]:
    return [StreamInfo(pid, stream_type, tag) for pid, stream_type, tag in raw]


def _event(raw: dict) -> EventInfo:
    return EventInfo(
        event_id=raw["event_id"], start=_time(raw["start"]), duration=_duration(raw["duration"]),
        name=raw["name"], description=raw["description"], extended=raw["extended"], genres=list(raw["genres"]),
        audio_components=[AudioComponent(*a) for a in raw["audio_components"]],
    )


# ---- 録画した番組の選び方 ---------------------------------------------------

def choose_event(events: List[EventInfo], recording_start: Optional[datetime],
                 recording_end: Optional[datetime]) -> Optional[EventInfo]:
    """録画の時間帯との重なりが最も大きい番組。時間帯が分からなければ None。

    長さが未定の番組は、録画の終わりまで続くとみなす。
    """
    if recording_start is None or recording_end is None or recording_end <= recording_start:
        return None
    best: Optional[EventInfo] = None
    best_overlap = timedelta(0)
    for event in events:
        if event.start is None:
            continue
        end = event.end or recording_end
        overlap = min(end, recording_end) - max(event.start, recording_start)
        if overlap > best_overlap:
            best, best_overlap = event, overlap
    return best


def read_program_info(path: Path) -> Optional[ProgramInfo]:
    """TS の番組情報を読む。TS でない・番組情報が無い時は None。

    読むのはファイルの先頭・中ほど・末尾の数十 MB だけ(6GB の録画で1秒程度)。読むのは Rust の部品
    (無い・版が合わない時は E506)。
    """
    raw = native_module().read_program_info_raw(str(Path(path)))
    if raw is None:
        return None
    sid = raw["service_id"]
    info = ProgramInfo(service_id=sid, streams=_streams(raw["streams"]), pcr_pid=raw["pcr_pid"],
                       packet_size=raw["packet_size"])
    if raw["programme_streams"] is not None:
        info.programme_streams = _streams(raw["programme_streams"])
    if raw["sdt"] is not None:
        info.transport_stream_id, info.original_network_id, info.service_name, info.provider_name = raw["sdt"]

    first, last, end_offset = raw["first"], raw["last"], raw["end_offset"]
    if first is not None:
        info.recording_start = _time(first[0]) - timedelta(seconds=first[1] or 0.0)
    if last is not None and last[1] is not None and end_offset is not None:
        info.recording_end = _time(last[0]) + timedelta(seconds=end_offset - last[1])
    elif last is not None:
        info.recording_end = _time(last[0])

    # 番組は event_id ごとに、後で読んだ(新しい)情報を使う
    events: Dict[int, EventInfo] = {}
    head_eits, middle_eits, tail_eits = raw["head_eits"], raw["middle_eits"], raw["tail_eits"]
    for eit in head_eits + middle_eits + tail_eits:
        if eit["service_id"] != sid:
            continue
        if info.original_network_id is None:
            info.original_network_id = eit["original_network_id"]
            info.transport_stream_id = eit["transport_stream_id"]
        for event in eit["events"]:
            events[event["event_id"]] = _event(event)
    info.events = sorted(events.values(), key=lambda e: (e.start is None, e.start or datetime.min.replace(tzinfo=JST)))
    info.event = choose_event(info.events, info.recording_start, info.recording_end)
    if info.event is None:
        # 録画の時間帯が分からない時は、元と同じく中ほどの「現在の番組」
        for eit in middle_eits + head_eits:
            if eit["service_id"] == sid and eit["section_number"] == 0 and eit["events"]:
                info.event = events[eit["events"][0]["event_id"]]
                break
    if not info.events and info.recording_start is None:
        # 放送の録画には EIT と TOT が必ずある。どちらも無い TS(ffmpeg で作った TS、この
        # アプリの出力)の SDT は ARIB の文字コードではなく、局名が文字化けした
        return None
    return info
