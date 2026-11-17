"""先頭の head 秒は音声 1 つ(PID 0x112、440Hz)、その後は音声 2 つ(0x112 と 0x113、880Hz)の TS を作る。

GR-B の録画と同じ形(番組が始まると PMT が更新されて 2 つ目の音声 component_tag 0x11 が加わる)。
2 つの TS をつなぎ、後ろの PMT の version_number を 1 にして CRC を付け直す(版が同じだと demuxer が
更新を読まない)。番組情報(EIT)は無い。
"""

import subprocess
import tempfile
from pathlib import Path

PMT_PID = 0x1F0
FIRST_PID, SECOND_PID = 0x112, 0x113


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _make(path: Path, seconds: float, two: bool, offset: float) -> None:
    inputs = ["-f", "lavfi", "-i", f"testsrc2=size=720x480:rate=30000/1001:duration={seconds}",
              "-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:duration={seconds}"]
    maps = ["-map", "0:v", "-map", "1:a"]
    ids = ["-streamid", "0:0x111", "-streamid", f"1:{FIRST_PID:#x}"]
    if two:
        inputs += ["-f", "lavfi", "-i", f"sine=f=880:sample_rate=48000:duration={seconds}"]
        maps += ["-map", "2:a"]
        ids += ["-streamid", f"2:{SECOND_PID:#x}"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, *maps,
                    "-c:v", "mpeg2video", "-q:v", "2", "-g", "15", "-bf", "2", "-flags", "+ilme+ildct", "-vf", "setfield=tff",
                    "-c:a", "aac", "-b:a", "96k",
                    *ids, "-mpegts_service_id", "1", "-mpegts_pmt_start_pid", f"{PMT_PID:#x}",
                    "-output_ts_offset", f"{offset}", "-muxdelay", "0", "-f", "mpegts", str(path)], check=True)


def _bump_pmt_version(data: bytearray) -> None:
    for i in range(0, len(data) - 187, 188):
        pkt = data[i:i + 188]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != PMT_PID or not (pkt[1] & 0x40):
            continue
        pos = 4 + (1 + pkt[4] if pkt[3] & 0x20 else 0)
        pos += 1 + pkt[pos]  # pointer_field
        length = ((pkt[pos + 1] & 0x0F) << 8) | pkt[pos + 2]
        section = bytearray(pkt[pos:pos + 3 + length])
        section[5] = (section[5] & 0xC1) | (1 << 1)  # version_number = 1
        section[-4:] = _crc32_mpeg(bytes(section[:-4])).to_bytes(4, "big")
        data[i + pos:i + pos + len(section)] = section


def make_two_audio_ts(out: Path, head: float = 20.0, tail: float = 30.0) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a.ts", Path(tmp) / "b.ts"
        _make(a, head, False, 1.4)
        _make(b, tail, True, 1.4 + head)
        second = bytearray(b.read_bytes())
        _bump_pmt_version(second)
        out.write_bytes(a.read_bytes() + bytes(second))
    return out
