"""コピーした H.264 の部分の POC を、前の部分に続くよう書き換える(native/src/h264.rs、ffmpeg/smart_cut.py)。

放送の H.264 はキーフレームが IDR でない I のことがあり、前の部分の後ろにつなぐと I の POC が前の部分より前になり、
ffmpeg のデコーダーが後ろの B フレームを捨てた(Smart Cut で約 150 枚、Simple Copy で 89 枚、2026-11-17)。
書き換えはファイルの中の見出しのビットだけで、部分の中の絵は同じにデコードされること、POC の下位がずれること、
書き換えの要らない部分(IDR から始まる)は触らないことを、実際の x264 の出力で確かめる。
"""

import hashlib
import re
import shutil
import subprocess

import pytest

from tests._media_check import FRAME_PATTERN
from ts_smartcut.autocut.native import native_module


def _source(path, x264_params, frames=120):
    """インターレース(TFF)の x264 の TS。キーフレームは 30 フレームごと。"""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"nullsrc=size=320x240:rate=60000/1001,format=yuv420p,{FRAME_PATTERN}",
         "-vf", "tinterlace=interleave_top,setfield=tff", "-frames:v", str(frames),
         "-c:v", "libx264", "-flags", "+ilme+ildct", "-bf", "3", "-refs", "4", "-g", "30", "-preset", "veryfast",
         "-x264-params", x264_params, str(path)],
        check=True, capture_output=True,
    )
    return path


def _copy_from_keyframe(source, path, number):
    """number 番目のキーフレームから後ろを、Smart Cut の Body と同じくパケットの表示時刻でコピーする。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
         "-of", "csv=p=0", str(source)], capture_output=True, text=True, check=True,
    ).stdout
    keys = [float(line.split(",")[0]) for line in out.split() if "K" in line.split(",")[1]]
    start = keys[number] - 0.001
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-copyts", "-i", str(source), "-map", "0:v:0", "-c", "copy",
         "-bsf:v", f"noise=drop=lt(pts*tb\\,{start:.6f})", str(path)],
        check=True, capture_output=True,
    )
    return path


def _frame_hashes(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        check=True, capture_output=True,
    ).stdout
    size = 320 * 240 * 3 // 2
    return [hashlib.md5(raw[i:i + size]).hexdigest() for i in range(0, len(raw), size)]


def _poc_lsbs(path):
    trace = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-c", "copy", "-bsf:v", "trace_headers", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    ).stderr
    width = int(re.search(r"log2_max_pic_order_cnt_lsb_minus4\s+\S+ = (\d+)", trace).group(1)) + 4
    return width, [int(m) for m in re.findall(r"pic_order_cnt_lsb\s+\S+ = (\d+)", trace)]


@pytest.fixture(scope="module")
def native():
    return native_module()


def test_copied_part_continues_the_previous_part(native, tmp_path):
    source = _source(tmp_path / "open.ts", "open-gop=1:scenecut=0")
    part = _copy_from_keyframe(source, tmp_path / "part.ts", 2)
    original = tmp_path / "original.ts"
    shutil.copy(part, original)
    width, before = _poc_lsbs(original)
    max_lsb = 1 << width

    added, broken = native.h264_continue_order(str(part), 3, 0)

    assert added is not None
    assert broken == 0
    # 部分の中の絵は同じにデコードされる(POC どうしの差は変わらない)
    assert _frame_hashes(part) == _frame_hashes(original)
    width_after, after = _poc_lsbs(part)
    assert width_after == width
    assert after == [(lsb + added) % max_lsb for lsb in before]
    # 最初の I は、前の部分の最後の参照の絵(下位 3)の 2 以上・半分以下だけ後
    assert 2 <= (after[0] - 3) % max_lsb <= max_lsb // 2


def test_tail_order_of_a_part(native, tmp_path):
    # Smart Cut の Head と同じ形(B フレームなし)。最後の参照の絵の後に表示される絵は無い
    source = tmp_path / "head.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"nullsrc=size=320x240:rate=60000/1001,format=yuv420p,{FRAME_PATTERN}",
         "-vf", "tinterlace=interleave_top,setfield=tff", "-frames:v", "5",
         "-c:v", "libx264", "-flags", "+ilme+ildct", "-bf", "0", "-preset", "veryfast", str(source)],
        check=True, capture_output=True,
    )
    width, lsbs = _poc_lsbs(source)
    assert native.h264_tail_order(str(source)) == (width, lsbs[-1], 0)
    # B フレームのある部分: 最後の参照の絵より後に表示される B フレームの分だけ先がある
    open_gop = _source(tmp_path / "open.ts", "open-gop=1:scenecut=0", frames=40)
    _, _, ahead = native.h264_tail_order(str(open_gop))
    assert ahead >= 0


def test_part_starting_with_an_idr_is_left_alone(native, tmp_path):
    source = _source(tmp_path / "closed.ts", "scenecut=0")  # キーフレームはすべて IDR
    part = _copy_from_keyframe(source, tmp_path / "part.ts", 1)
    data = part.read_bytes()
    assert native.h264_continue_order(str(part), 3, 0) == (None, 0)
    assert part.read_bytes() == data


def test_non_h264_part_is_refused(native, tmp_path):
    source = tmp_path / "mpeg2.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30000/1001",
         "-frames:v", "10", "-c:v", "mpeg2video", str(source)], check=True, capture_output=True,
    )
    with pytest.raises(ValueError, match="no H.264 video"):
        native.h264_tail_order(str(source))


def test_tail_order_reads_back_to_the_parameter_sets(native, tmp_path):
    # 前の部分は終わりの 4 MiB だけを読む。SPS・PPS が先頭にしか無い (キーフレームが 1 つだけの) 大きな部分では、
    # 読む所を前へ広げて最後の参照の絵を見つける
    source = tmp_path / "long.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=60000/1001",
         "-vf", "noise=alls=60:allf=t,tinterlace=interleave_top,setfield=tff", "-frames:v", "150",
         "-c:v", "libx264", "-flags", "+ilme+ildct", "-bf", "0", "-qp", "0", "-preset", "ultrafast",
         "-x264-params", "keyint=infinite:scenecut=0", str(source)],
        check=True, capture_output=True,
    )
    assert source.stat().st_size > 8 << 20
    assert source.read_bytes().count(bytes([0, 0, 1, 0x67])) == 1  # SPS (NAL 7) は先頭の 1 つだけ
    width, lsbs = _poc_lsbs(source)
    assert native.h264_tail_order(str(source)) == (width, lsbs[-1], 0)


def _break_a_slice_header(path, which):
    """which 番目のスライス (NAL 1) の見出しを、読めない Exp-Golomb (0 が 32 個より多い) にする。"""
    data = bytearray(path.read_bytes())
    found = [m.start() + 3 for m in re.finditer(rb"\x00\x00\x01[\x01\x21\x41\x61]", bytes(data))
             if (m.start() + 3) % 188 + 13 < 188]
    pos = found[which]
    data[pos + 1:pos + 13] = bytes([0, 0, 3]) * 4
    path.write_bytes(bytes(data))


def test_unreadable_slices_are_left_as_they_are(native, tmp_path):
    # 受信エラーで壊れた見出しのスライスは飛ばして数え、ほかのスライスは書き換える
    source = _source(tmp_path / "open.ts", "open-gop=1:scenecut=0")
    part = _copy_from_keyframe(source, tmp_path / "part.ts", 2)
    _break_a_slice_header(part, 4)

    added, broken = native.h264_continue_order(str(part), 3, 0)

    assert added is not None
    assert broken == 1


class _Set:
    def is_set(self):
        return True


def test_cancel_stops_without_changing_the_part(native, tmp_path):
    source = _source(tmp_path / "open.ts", "open-gop=1:scenecut=0")
    part = _copy_from_keyframe(source, tmp_path / "part.ts", 2)
    data = part.read_bytes()
    with pytest.raises(ValueError, match="cancelled"):
        native.h264_continue_order(str(part), 3, 0, cancel=_Set())
    with pytest.raises(ValueError, match="cancelled"):
        native.h264_tail_order(str(part), cancel=_Set())
    assert part.read_bytes() == data
