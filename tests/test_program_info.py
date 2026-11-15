"""TS の番組情報の読み取りと、録画した番組の選び方(Rust の native/src/program_info.rs・ts.rs と
broadcast/program_info.py、自動カット A1-2)。

TS は手で組み立てる(PAT・PMT・SDT・EIT・TOT のセクションと PCR のパケット)。TS の読み方(CRC・欠けた
パケット・重複・パケットをまたぐセクション・同期のずれ)も、組み立てた TS を読んだ結果で確かめる。手元に
実録画と rplstool-cs の CLI があれば、CLI の出力と照合する。
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

import pytest

from ts_smartcut.broadcast import program_info as pi
from ts_smartcut.broadcast.program_info import JST, EventInfo, choose_event, read_program_info

PCR_WRAP = (1 << 33) * 300

LS1 = 0x0E  # 英数に切り替える(ARIB の文字コード)


def arib(text: str) -> bytes:
    return bytes([LS1]) + text.encode("ascii")


def _opt(text: str) -> bytes:
    return arib(text) if text else b""


# ---- TS の組み立て ----------------------------------------------------------

def _crc_table_crc(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def section(table_id: int, ext: int, body: bytes, section_number: int = 0, version: int = 0,
            syntax: bool = True) -> bytes:
    if not syntax:  # TOT: 長い形式ではないが CRC は付く
        head = bytes([table_id, 0x30 | ((len(body) + 4) >> 8), (len(body) + 4) & 0xFF])
        data = head + body
    else:
        rest = bytes([ext >> 8, ext & 0xFF, 0xC1 | (version << 1), section_number, 1]) + body
        length = len(rest) + 4
        data = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF]) + rest
    crc = _crc_table_crc(data)
    return data + crc.to_bytes(4, "big")


def descriptor(tag: int, body: bytes) -> bytes:
    return bytes([tag, len(body)]) + body


def mjd_bcd(t: datetime) -> bytes:
    mjd = (t.date() - datetime(1858, 11, 17).date()).days
    bcd = lambda v: ((v // 10) << 4) | (v % 10)  # noqa: E731
    return bytes([mjd >> 8, mjd & 0xFF, bcd(t.hour), bcd(t.minute), bcd(t.second)])


def duration_bcd(d: timedelta) -> bytes:
    s = int(d.total_seconds())
    bcd = lambda v: ((v // 10) << 4) | (v % 10)  # noqa: E731
    return bytes([bcd(s // 3600), bcd(s // 60 % 60), bcd(s % 60)])


SERVICE_ID = 0x0123
ONID = 0x0004  # BS
TSID = 0x4010
PMT_PID = 0x01F0
PCR_PID = 0x01FF
VIDEO_PID = 0x0100


def pat() -> bytes:
    body = bytes([0, 0, 0xE0, 0x10]) + bytes([SERVICE_ID >> 8, SERVICE_ID & 0xFF,
                                              0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF])
    return section(0x00, TSID, body)


def pmt() -> bytes:
    es = bytes([0x02, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 3]) + descriptor(0x52, b"\x00")
    es += bytes([0x0F, 0xE1, 0x10, 0xF0, 0])
    body = bytes([0xE0 | (PCR_PID >> 8), PCR_PID & 0xFF, 0xF0, 0]) + es
    return section(0x02, SERVICE_ID, body)


def sdt() -> bytes:
    service = descriptor(0x48, bytes([0x01]) + bytes([len(arib("PROV"))]) + arib("PROV")
                         + bytes([len(arib("TEST BS"))]) + arib("TEST BS"))
    entry = bytes([SERVICE_ID >> 8, SERVICE_ID & 0xFF, 0xFC, 0x80 | (len(service) >> 8), len(service) & 0xFF]) + service
    body = bytes([ONID >> 8, ONID & 0xFF, 0xFF]) + entry
    return section(0x42, TSID, body)


def event(event_id: int, start: datetime, duration: timedelta, name: str, text: str,
          items: List[tuple] = (), genre: int = 0x70) -> bytes:
    descs = descriptor(0x4D, b"jpn" + bytes([len(arib(name))]) + arib(name) + bytes([len(arib(text))]) + arib(text))
    if items:
        loop = b"".join(bytes([len(_opt(h))]) + _opt(h) + bytes([len(arib(v))]) + arib(v) for h, v in items)
        descs += descriptor(0x4E, bytes([0x00]) + b"jpn" + bytes([len(loop)]) + loop + b"\x00")
    descs += descriptor(0x54, bytes([genre, 0xFF, 0xE0, 0xFF]))
    return (bytes([event_id >> 8, event_id & 0xFF]) + mjd_bcd(start) + duration_bcd(duration)
            + bytes([0x80 | (len(descs) >> 8), len(descs) & 0xFF]) + descs)


def eit(section_number: int, ev: bytes, version: int = 0) -> bytes:
    body = bytes([TSID >> 8, TSID & 0xFF, ONID >> 8, ONID & 0xFF, 0x01, 0x4E]) + ev
    return section(0x4E, SERVICE_ID, body, section_number=section_number, version=version)


def tot(t: datetime) -> bytes:
    return section(0x73, 0, mjd_bcd(t) + b"\xF0\x00", syntax=False)


class TsWriter:
    def __init__(self) -> None:
        self.packets: List[bytes] = []
        self.cc: Dict[int, int] = {}

    def _next_cc(self, pid: int) -> int:
        cc = self.cc.get(pid, -1) + 1 & 0x0F
        self.cc[pid] = cc
        return cc

    def sections(self, pid: int, *sections: bytes) -> None:
        """セクションを続けて詰める(1つのパケットに複数のセクションが入ることもある)。"""
        data = b"".join(sections)
        first = True
        while data or first:
            if first:
                payload = b"\x00" + data[:183]
                data = data[183:]
            else:
                payload = data[:184]
                data = data[184:]
            header = bytes([0x47, (0x40 if first else 0) | pid >> 8, pid & 0xFF, 0x10 | self._next_cc(pid)])
            self.packets.append(header + payload + b"\xFF" * (184 - len(payload)))
            first = False

    def pcr(self, value: int) -> None:
        base, ext = value // 300, value % 300
        field = bytes([0x10, base >> 25 & 0xFF, base >> 17 & 0xFF, base >> 9 & 0xFF, base >> 1 & 0xFF,
                       (base & 1) << 7 | 0x7E | ext >> 8, ext & 0xFF])
        af = bytes([183]) + field + b"\xFF" * (183 - len(field))
        self.packets.append(bytes([0x47, PCR_PID >> 8, PCR_PID & 0xFF, 0x20 | self._next_cc(PCR_PID)]) + af)

    def filler(self, count: int) -> None:
        self.packets += [bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xFF" * 184] * count

    def write(self, path: Path, packet_size: int = 188) -> Path:
        prefix = b"\x00\x00\x00\x00" if packet_size == 192 else b""
        path.write_bytes(b"".join(prefix + p for p in self.packets))
        return path


REC_START = datetime(2026, 10, 1, 0, 59, 55, tzinfo=JST)
PREV = (100, REC_START - timedelta(minutes=29, seconds=55), timedelta(minutes=30))  # 録画の 5 秒目まで
TARGET = (101, REC_START + timedelta(seconds=5), timedelta(seconds=80))
NEXT = (102, REC_START + timedelta(seconds=85), timedelta(minutes=6))


def make_recording(path: Path, pcr_origin: int = 10 * 27_000_000, packet_size: int = 188, filler: int = 20) -> Path:
    """100 秒の録画。0.1 秒ごとに PCR、先頭と末尾に SI、5 秒目と 95 秒目に TOT。

    録画は前の番組の終わり 5 秒・本編 80 秒・次の番組の始まり 15 秒。filler は 0.1 秒ごとの詰め物の
    パケットの数(400 で約 75MB: 先頭・中ほど・末尾を分けて読む大きさ)。
    """
    w = TsWriter()
    w.filler(3)  # 録画の先頭は SI の途中から
    for tick in range(1001):
        w.pcr((pcr_origin + tick * 2_700_000) % PCR_WRAP)
        if tick == 1:
            w.sections(0x0000, pat())
            w.sections(PMT_PID, pmt())
            w.sections(0x0011, sdt())
            w.sections(0x0012, eit(0, event(*PREV, "PREV", "prev text")),
                       eit(1, event(*TARGET, "TARGET", "target text", [("Cast", "A/B"), ("Story", "s1")])))
        if tick == 50:
            w.sections(0x0014, tot(REC_START + timedelta(seconds=5)))
        if tick == 500:
            w.sections(PMT_PID, pmt())  # 番組の途中の PMT (ProgramInfo.programme_streams)
            w.sections(0x0012, eit(0, event(*TARGET, "TARGET", "target text", [("Cast", "A/B"), ("Story", "s1")])),
                       eit(1, event(*NEXT, "NEXT", "next text")))
        if tick == 950:
            w.sections(0x0014, tot(REC_START + timedelta(seconds=95)))
            w.sections(0x0012, eit(0, event(*NEXT, "NEXT", "next text", genre=0x81)))
        w.filler(filler)
    return w.write(path, packet_size)


# ---- TS の読み方(CRC・パケット・セクション) -----------------------------------

def _recording_with(path: Path, *extra) -> Path:
    """先頭に PAT・PMT・SDT・TOT と、extra(パケットを足す関数)を並べた短い録画(TOT 2 つの間に PCR 1 秒)。"""
    w = TsWriter()
    w.pcr(0)
    w.sections(0x0000, pat())
    w.sections(PMT_PID, pmt())
    w.sections(0x0011, sdt())
    w.sections(0x0014, tot(REC_START + timedelta(seconds=5)))
    for add in extra:
        add(w)
    w.pcr(27_000_000)
    w.sections(0x0014, tot(REC_START + timedelta(seconds=6)))
    return w.write(path)


def test_a_section_with_a_bad_crc_is_dropped(tmp_path):
    good = eit(0, event(*TARGET, "TARGET", "target text"))
    bad = eit(1, event(200, REC_START, timedelta(seconds=60), "BAD", "x"))
    bad = bad[:30] + bytes([bad[30] ^ 0xFF]) + bad[31:]
    info = read_program_info(_recording_with(tmp_path / "rec.ts", lambda w: w.sections(0x12, bad, good)))
    assert [e.name for e in info.events] == ["TARGET"]


def test_sections_spanning_packets_and_sharing_a_packet(tmp_path):
    # 1 つのパケットに TOT が 2 つ、EIT は 3 つのパケットにまたがる
    big = eit(0, event(*TARGET, "TARGET", "x" * 200, [("Story", "y" * 200)]))
    info = read_program_info(_recording_with(
        tmp_path / "rec.ts",
        lambda w: w.sections(0x14, tot(REC_START + timedelta(seconds=1)), tot(REC_START + timedelta(seconds=2))),
        lambda w: w.sections(0x12, big)))
    assert info.recording_start == REC_START + timedelta(seconds=5)  # 最初の TOT(その位置まで PCR は進んでいない)
    assert info.recording_end == REC_START + timedelta(seconds=6)  # 最後の TOT
    assert info.events[0].description == "x" * 200
    assert info.events[0].extended == "Story\n" + "y" * 200


def test_a_lost_packet_drops_only_that_section(tmp_path):
    lost = eit(0, event(200, REC_START, timedelta(seconds=60), "LOST", "x" * 200))
    whole = eit(1, event(*TARGET, "TARGET", "x" * 200))

    def add(w):
        w.sections(0x12, lost)
        del w.packets[-2]  # 途中のパケットが欠けた(連続性カウンタが飛ぶ)
        w.sections(0x12, whole)

    info = read_program_info(_recording_with(tmp_path / "rec.ts", add))
    assert [e.name for e in info.events] == ["TARGET"]


def test_duplicate_packets_are_ignored(tmp_path):
    def add(w):
        start = len(w.packets)
        w.sections(0x12, eit(0, event(*TARGET, "TARGET", "x" * 200)))
        w.packets.insert(start + 1, w.packets[start])  # 同じパケットの再送

    info = read_program_info(_recording_with(tmp_path / "rec.ts", add))
    assert [e.name for e in info.events] == ["TARGET"]


def test_continued_extended_items_and_genres(tmp_path):
    # 項目名の無い項目は前の項目の続き。元と同じく「項目名\n本文\n\n項目名\n本文」。0xE0(番組付属情報)のジャンルは除く
    ev = eit(1, event(*TARGET, "TARGET", "target text", [("Cast", "A/B"), ("", "+C"), ("Story", "s1")]))
    info = read_program_info(_recording_with(tmp_path / "rec.ts", lambda w: w.sections(0x12, ev)))
    assert (info.service_id, info.original_network_id, info.transport_stream_id) == (SERVICE_ID, ONID, TSID)
    target = info.events[0]
    assert (target.event_id, target.start, target.duration) == (101, TARGET[1], TARGET[2])
    assert (target.name, target.description) == ("TARGET", "target text")
    assert target.extended == "Cast\nA/B+C\n\nStory\ns1"
    assert target.genres == [0x70]


def test_undefined_start_and_duration(tmp_path):
    # 開始時刻・長さがすべて 1 のビット(未定)
    raw = bytearray(event(*NEXT, "NEXT", "next text"))
    raw[2:10] = b"\xff" * 8
    info = read_program_info(_recording_with(tmp_path / "rec.ts", lambda w: w.sections(0x12, eit(0, bytes(raw)))))
    assert (info.events[0].start, info.events[0].duration) == (None, None)


@pytest.mark.parametrize("packet_size", [188, 192])
def test_broken_sync_is_found_again(tmp_path, packet_size):
    # 先頭にごみ、途中に半端なバイト(同期のずれ)があっても、同期を取り直して読み続ける
    data = make_recording(tmp_path / "rec.ts", packet_size=packet_size).read_bytes()
    broken = data[:5] + data[packet_size * 3 + 5:packet_size * 100] + b"\x00" * 77 + data[packet_size * 100:]
    (tmp_path / "broken.ts").write_bytes(broken)
    info = read_program_info(tmp_path / "broken.ts")
    assert info.packet_size == packet_size
    _check_recording(info)


# ---- 番組情報 ---------------------------------------------------------------

def test_mjd():
    assert pi.mjd_to_date(0xC079) == datetime(1993, 10, 13).date()  # EN 300 468 の例


def test_broadcast_type():
    assert pi.broadcast_type(0x0004) == "bs"
    assert pi.broadcast_type(0x0006) == pi.broadcast_type(0x0007) == "cs"
    assert pi.broadcast_type(0x7FE0) == "terrestrial"
    assert pi.broadcast_type(None) == pi.broadcast_type(0x000A) == "unknown"


def _ev(event_id, start_s, length_s):
    start = REC_START + timedelta(seconds=start_s)
    return EventInfo(event_id, start, None if length_s is None else timedelta(seconds=length_s))


def test_choose_event_picks_the_largest_overlap():
    end = REC_START + timedelta(seconds=100)
    prev, target, nxt = _ev(1, -1800, 1805), _ev(2, 5, 80), _ev(3, 85, 360)
    assert choose_event([prev, target, nxt], REC_START, end) is target
    # 録画の大半が前の番組(番組の終わり近くから録画した)
    assert choose_event([_ev(1, -100, 160), _ev(2, 60, 600)], REC_START, end).event_id == 1
    # 長さが未定の番組は録画の終わりまで続くとみなす
    assert choose_event([prev, _ev(2, 5, None)], REC_START, end).event_id == 2
    # 時間帯が分からない・重なりが無い
    assert choose_event([target], None, end) is None
    assert choose_event([_ev(1, 200, 60)], REC_START, end) is None


def _check_recording(info):
    assert info.service_id == SERVICE_ID
    assert (info.original_network_id, info.transport_stream_id) == (ONID, TSID)
    assert info.broadcast_type == "bs"
    assert (info.service_name, info.provider_name) == ("TEST BS", "PROV")
    assert info.pcr_pid == PCR_PID
    assert [s.pid for s in info.streams] == [VIDEO_PID, 0x110]
    # 録画開始 = 最初の TOT の時刻 − その位置までの PCR の進み(5 秒)
    assert abs((info.recording_start - REC_START).total_seconds()) < 0.2
    assert abs((info.recording_end - (REC_START + timedelta(seconds=100))).total_seconds()) < 0.2
    assert [e.event_id for e in info.events] == [100, 101, 102]
    assert info.event.event_id == 101
    assert info.event.name == "TARGET"
    assert info.event.extended == "Cast\nA/B\n\nStory\ns1"
    assert info.events[2].genres == [0x81]  # 末尾で読んだ新しい情報


@pytest.mark.parametrize("packet_size", [188, 192])
def test_read_program_info_whole_file_in_one_chunk(tmp_path, packet_size):
    info = read_program_info(make_recording(tmp_path / "rec.ts", packet_size=packet_size))
    assert info.packet_size == packet_size
    _check_recording(info)


def test_read_program_info_head_middle_and_tail(tmp_path):
    # 先頭の 1 回の読み込み(32MB)より大きい録画は、先頭・中ほど・末尾だけを読む。PCR は途中で一周する。
    # 約 75MB: 最後の TOT が、先頭として読む 32MB の外(末尾として読む所)に入る大きさ
    path = make_recording(tmp_path / "rec.ts", pcr_origin=PCR_WRAP - 50 * 27_000_000, filler=400)
    assert path.stat().st_size > 32 * 1024 * 1024
    info = read_program_info(path)
    _check_recording(info)
    assert [s.pid for s in info.programme_streams] == [VIDEO_PID, 0x110]  # 中ほどの PMT


def test_not_a_ts(tmp_path):
    path = tmp_path / "x.ts"
    path.write_bytes(b"\x00" * 10000)
    assert read_program_info(path) is None


def test_ts_without_si(tmp_path):
    w = TsWriter()
    w.filler(100)
    assert read_program_info(w.write(tmp_path / "x.ts")) is None


# ---- 実録画と rplstool-cs の CLI ---------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_RECORDINGS = sorted(p for p in _ROOT.glob("*.ts") if "_cut" not in p.stem)


@pytest.mark.skipif(not _RECORDINGS, reason="実録画が無い")
@pytest.mark.parametrize("path", _RECORDINGS, ids=lambda p: p.stem[-12:])
def test_real_recording_matches_rplstool_cli(path):
    import tools.check_program_info as check

    info = read_program_info(path)
    assert info is not None and info.event is not None
    # 録画の時間帯は、ファイルの長さ(PCR)と1秒程度で合う
    assert info.recording_start < info.event.start + info.event.duration
    assert info.recording_end > info.event.start
    dll = check.find_cli()
    if dll is None or shutil.which("dotnet") is None:
        pytest.skip("rplstool-cs の CLI が無い")
    cli = check.run_cli(dll, path)
    assert cli is not None
    assert check.compare(info, cli) == []


def test_ffmpeg_made_ts_without_eit_and_tot_has_no_program_info(tmp_path):
    """放送の録画でない TS(ffmpeg で作った TS、このアプリの出力)の SDT は ARIB の文字コード
    ではない。EIT も TOT も無いので番組情報は無しとする(局名が文字化けした、2026-10-09)。"""
    import subprocess

    path = tmp_path / "ffmpeg.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=320x240:r=30",
         "-c:v", "mpeg2video", str(path)],
        check=True,
    )
    assert read_program_info(path) is None
