"""音声の形式・字幕の変化 (autocut/stream_events.py、解析は Rust の StreamScanner。自動カット計画 A3)。

TS は手で組み立てる(PAT・PMT と、映像・音声・字幕の PES の始まりのパケット)。
"""

from pathlib import Path
from typing import List, Optional

import numpy as np
import pytest

import ts_smartcut_native as native
from ts_smartcut.autocut.stream_events import StreamEvents, scan_stream_events
from tests.test_program_info import TsWriter, descriptor, section

SERVICE_ID = 0x0400
PMT_PID = 0x01F0
VIDEO_PID = 0x0100
AUDIO_PID = 0x0110
CAPTION_PID = 0x0130
VIDEO_START = 10_000.0  # 秒(放送の PTS は大きい)


def _pat() -> bytes:
    body = bytes([SERVICE_ID >> 8, SERVICE_ID & 0xFF, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF])
    return section(0x00, 0x7FE0, body)


def _pmt(with_captions: bool, version: int) -> bytes:
    es = bytes([0x02, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 3]) + descriptor(0x52, b"\x00")
    es += bytes([0x0F, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 3]) + descriptor(0x52, b"\x10")
    if with_captions:
        es += bytes([0x06, 0xE0 | (CAPTION_PID >> 8), CAPTION_PID & 0xFF, 0xF0, 3]) + descriptor(0x52, b"\x30")
    body = bytes([0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0]) + es
    return section(0x02, SERVICE_ID, body, version=version)


def _pts_bytes(pts: int) -> bytes:
    return bytes([
        0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1,
        (pts >> 7) & 0xFF, ((pts << 1) & 0xFE) | 1,
    ])


def _pes(w: TsWriter, pid: int, stream_id: int, seconds: Optional[float], data: bytes) -> None:
    if seconds is None:
        header = bytes([0, 0, 1, stream_id, 0, 0, 0x80, 0x00, 0])
    else:
        pts = int(round((VIDEO_START + seconds) * 90000)) % (1 << 33)
        header = bytes([0, 0, 1, stream_id, 0, 0, 0x80, 0x80, 5]) + _pts_bytes(pts)
    payload = header + data
    w.packets.append(bytes([0x47, 0x40 | pid >> 8, pid & 0xFF, 0x10 | w._next_cc(pid)])
                     + payload + b"\xFF" * (184 - len(payload)))


def _adts(channels: int, sfi: int = 3) -> bytes:
    length = 300
    return bytes([
        0xFF, 0xF1, (1 << 6) | (sfi << 2) | (channels >> 2), ((channels & 3) << 6) | (length >> 11),
        (length >> 3) & 0xFF, ((length & 7) << 5) | 0x1F, 0xFC,
    ]) + b"\x21" * 20


def _caption(statement: bool) -> bytes:
    group_id = 0x01 if statement else 0x00
    return bytes([0x80, 0xFF, 0xF0, group_id << 2, 0x00, 0x00, 0x10])


def make_ts(path: Path, sfi_from_2s: int = 3) -> Path:
    """6 秒。0.1 秒ごとに映像・音声の PES。音声は 3〜5 秒が 5.1ch (6)、1.0 秒に
    1回だけヘッダに似た偽の値。字幕は 2.0 秒に字幕文、1.0・3.0 秒に管理データ。
    4.0 秒の PMT で字幕の ES が無くなる(その後の字幕の PES は数えない)。
    sfi_from_2s: 2.0 秒からのサンプリング周波数(既定は変えない)。"""
    w = TsWriter()
    w.filler(2)
    for tick in range(60):
        t = tick / 10
        if tick % 5 == 0:
            w.sections(0x0000, _pat())
            w.sections(PMT_PID, _pmt(with_captions=t < 4.0, version=0 if t < 4.0 else 1))
        _pes(w, VIDEO_PID, 0xE0, t, b"\x00\x00\x01\xB3")
        if tick == 10:
            channels, sfi = 7, 5  # 音声のデータの途中に見える偽のヘッダ(実録画の BS-A)
        else:
            channels, sfi = (6 if 3.0 <= t < 5.0 else 2), (3 if t < 2.0 else sfi_from_2s)
        _pes(w, AUDIO_PID, 0xC0, t - 0.03, _adts(channels, sfi))
        if tick in (10, 20, 30, 45):
            _pes(w, CAPTION_PID, 0xBD, t, _caption(statement=tick in (20, 45)))
        w.filler(3)
    return w.write(path)


@pytest.fixture
def ts_path(tmp_path):
    return make_ts(tmp_path / "streams.ts")


def _check(events) -> None:
    assert events.complete
    assert [t for t, _ in events.pmt_changes] == pytest.approx([0.0, 3.9], abs=0.11)
    assert [s[0] for s in events.pmt_changes[0][1]] == [VIDEO_PID, AUDIO_PID, CAPTION_PID]
    assert [s[0] for s in events.pmt_changes[1][1]] == [VIDEO_PID, AUDIO_PID]
    # 偽のヘッダは無視し、本当の変化(2 回続いた値)の最初の位置を記録する
    assert [c for _, c in events.audio_channels] == [2, 6, 2]
    assert [t for t, _ in events.audio_channels] == pytest.approx([-0.03, 2.97, 4.97], abs=1e-3)
    # 字幕文だけ。管理データと、PMT から字幕の ES が無くなった後は数えない
    assert list(events.caption_times) == pytest.approx([2.0], abs=1e-3)


def test_scan_finds_pmt_audio_and_caption_changes(ts_path):
    _check(scan_stream_events(ts_path, VIDEO_START, SERVICE_ID))


def test_scan_without_service_id_uses_the_first_program(ts_path):
    _check(scan_stream_events(ts_path, VIDEO_START))


@pytest.mark.parametrize("piece", [1000, 188 * 7 + 5, 64 * 1024])
def test_scanner_accepts_data_split_anywhere(ts_path, piece):
    data = ts_path.read_bytes()
    scanner = native.StreamScanner(SERVICE_ID, VIDEO_START)
    for i in range(0, len(data), piece):
        assert scanner.feed_bytes(data[i:i + piece]) is None
    _check(StreamEvents.from_native(*scanner.result(), complete=True))


@pytest.mark.parametrize("piece", [1000, 188 * 7 + 5, 4000, 64 * 1024])
def test_scanner_accepts_data_split_anywhere_after_a_resync(ts_path, piece):
    """受信エラーでパケットの並びがずれた後も、どこで分けて渡しても同じ結果になる(前は同期を
    取り直した後、渡したデータの区切りごとに 1 パケットずつ落ちた)。"""
    data = ts_path.read_bytes()
    broken = data[:188 * 20] + bytes(100) + data[188 * 20:]
    scanner = native.StreamScanner(SERVICE_ID, VIDEO_START)
    for i in range(0, len(broken), piece):
        assert scanner.feed_bytes(broken[i:i + piece]) is None
    _check(StreamEvents.from_native(*scanner.result(), complete=True))


def test_channel_changes_after_a_sampling_rate_change(tmp_path):
    # 2.0 秒でサンプリング周波数が変わった後のチャンネル数の変化(3.0 秒の 5.1ch)を捨てていた(2026-11-17)
    path = make_ts(tmp_path / "rate.ts", sfi_from_2s=4)
    _check(scan_stream_events(path, VIDEO_START, SERVICE_ID))


def test_pts_wrap_is_handled(tmp_path):
    # 映像の先頭の直後に PTS が一周する(33 ビット、約 26.5 時間)
    global VIDEO_START
    saved = VIDEO_START
    VIDEO_START = (1 << 33) / 90000 - 1.0
    try:
        path = make_ts(tmp_path / "wrap.ts")
        events = scan_stream_events(path, VIDEO_START, SERVICE_ID)
    finally:
        VIDEO_START = saved
    assert [t for t, _ in events.audio_channels] == pytest.approx([-0.03, 2.97, 4.97], abs=1e-3)
    assert list(events.caption_times) == pytest.approx([2.0], abs=1e-3)


def test_not_a_ts(tmp_path):
    path = tmp_path / "x.ts"
    path.write_bytes(b"\x00" * 10000)
    events = scan_stream_events(path, 0.0)
    assert events.pmt_changes == [] and events.audio_channels == [] and len(events.caption_times) == 0
