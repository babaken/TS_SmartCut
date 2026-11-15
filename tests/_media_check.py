"""カット結果の検証ヘルパー（実ffmpeg/ffprobeで出力を解析する）。

フレーム数や「例外が出ないこと」だけでは、正しい枚数で中身が別のフレーム、
カット区間の音声の混入、つなぎ目の時刻の空きといった不具合を検出できない
(2026-10-04 実測: いずれも既存テストをすり抜けていた)。ここでは出力の
各フレームが元のどのフレームかを画像で照合し、音声パケットを元の時刻へ
逆引きして映像との同期を確かめる。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np

_W, _H = 64, 48

# フレーム番号(59.94fpsの元フレームN)を縦縞の2進数で描く。隣接フレームの差が
# 圧縮ノイズより桁違いに大きくなるため、照合を取り違えない。10ビット=1024。
FRAME_PATTERN = "geq=lum='255*mod(floor(N/pow(2\\,floor(X*10/W)))\\,2)':cb=128:cr=128"


def make_interlaced_source(
    path: Path, video_codec_args: List[str], audio_codec: str, seconds: float = 4,
    audio_lead: float = 0.0,
) -> Path:
    """フレームごとに一意な絵柄のインターレース(TFF, 29.97i)動画を作る。
    audio_lead > 0 のとき音声を映像より先に開始させる(放送TSに多い)。"""
    video = (f"nullsrc=duration={seconds}:size=320x240:rate=60000/1001,"
             f"format=yuv420p,{FRAME_PATTERN}")
    audio = f"anoisesrc=d={seconds + 1}:c=pink:r=48000:seed=11"
    args = ["ffmpeg", "-v", "error", "-y"]
    if audio_lead:
        args += ["-itsoffset", str(audio_lead)]
    args += [
        "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
        "-vf", "tinterlace=interleave_top,setfield=tff",
        *video_codec_args, "-c:a", audio_codec,
        "-t", str(seconds + audio_lead),
        str(path),
    ]
    subprocess.run(args, check=True, capture_output=True)
    return path


def _gray_frames(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
         "-vf", f"scale={_W}:{_H}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, _H, _W).astype(np.float32)


def _frames_meta(path: Path) -> List[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts_time,interlaced_frame,top_field_first", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["frames"]


def _audio_packets(path: Path) -> List[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_data_hash", "md5",
         "-show_entries", "packet=pts_time,data_hash", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["packets"]


@dataclass
class CutCheck:
    source_frame_count: int  # 元動画の実際のフレーム数(デコードして数えた値)
    source_frames: List[int]  # 出力の各フレームが対応する元のフレーム番号
    all_interlaced_tff: bool
    max_pts_step_error: float  # フレーム間隔の理想値からの最大ずれ(秒)
    audio_matched: int
    audio_leaked: int  # 出力映像のどこにも対応しない(=カット区間等の)音声パケット数
    audio_duplicated: int
    audio_max_sync_error: float  # 秒


def check_cut(source: Path, output: Path) -> CutCheck:
    src_frames, out_frames = _gray_frames(source), _gray_frames(output)
    matched = [int(np.argmin(((src_frames - f) ** 2).mean(axis=(1, 2)))) for f in out_frames]

    src_meta, out_meta = _frames_meta(source), _frames_meta(output)
    all_interlaced_tff = all(
        m.get("interlaced_frame") == 1 and m.get("top_field_first") == 1 for m in out_meta
    )
    src_pts = [float(m["pts_time"]) for m in src_meta]
    out_pts = [float(m["pts_time"]) for m in out_meta]
    step = float(np.median(np.diff(src_pts)))
    max_step_error = float(np.abs(np.diff(out_pts) - step).max()) if len(out_pts) > 1 else 0.0

    # 出力映像の各フレームについて「出力時刻 - 元時刻」。同じ連続区間内では一定。
    offsets = [(src_pts[s], out_pts[k] - src_pts[s]) for k, s in enumerate(matched)]

    def video_offset_at(src_t: float):
        nearest = min(offsets, key=lambda x: abs(x[0] - src_t))
        return nearest[1] if abs(nearest[0] - src_t) <= step else None

    src_audio = {p["data_hash"]: float(p["pts_time"]) for p in _audio_packets(source)}
    seen: Dict[float, int] = {}
    errors, leaked = [], 0
    for packet in _audio_packets(output):
        src_t = src_audio.get(packet["data_hash"])
        if src_t is None:
            continue
        seen[src_t] = seen.get(src_t, 0) + 1
        voff = video_offset_at(src_t)
        if voff is None:
            leaked += 1
            continue
        errors.append((float(packet["pts_time"]) - src_t) - voff)

    return CutCheck(
        source_frame_count=len(src_frames),
        source_frames=matched,
        all_interlaced_tff=all_interlaced_tff,
        max_pts_step_error=max_step_error,
        audio_matched=len(seen),
        audio_leaked=leaked,
        audio_duplicated=sum(1 for c in seen.values() if c > 1),
        audio_max_sync_error=float(np.abs(errors).max()) if errors else 0.0,
    )


def _pattern(offset: int) -> str:
    """フレーム番号(+offset)を縦縞の2進数で描く(区間ごとに番号が重ならないように)。"""
    return f"geq=lum='255*mod(floor((N+{offset})/pow(2\\,floor(X*10/W)))\\,2)':cb=128:cr=128"


def _encode_es(path: Path, video: str, codec_args: List[str]) -> bytes:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", video, *codec_args,
         "-f", "mpeg2video", str(path)],
        check=True, capture_output=True,
    )
    return path.read_bytes()


def _soft_telecine_es(path: Path, seconds: float, offset: int) -> bytes:
    """24pで符号化し、3:2のフィールド表示フラグを書き込んだMPEG-2 ES。

    ffmpegのMPEG-2エンコーダーはrepeat_first_fieldを書けないため、規格上の
    固定位置のビットを書き換える(frame_rate_code=29.97, progressive_sequence=0,
    各ピクチャのtop_field_first/repeat_first_field)。Bフレームなし(符号化順=
    表示順)。"""
    from ts_smartcut.ffmpeg.mpeg2_flags import PictureFlags, rewrite_field_flags

    video = (f"nullsrc=duration={seconds}:size=320x240:rate=24000/1001,"
             f"format=yuv420p,{_pattern(offset)}")
    data = _encode_es(path, video, ["-c:v", "mpeg2video", "-bf", "0", "-g", "12", "-b:v", "3M"])
    cadence = [(True, True), (False, False), (False, True), (True, False)]  # (tff, rff): T-B-T, B-T, B-T-B, T-B
    count = sum(1 for i in range(len(data) - 3) if data[i:i + 4] == b"\x00\x00\x01\x00")
    flags = [PictureFlags(True, *cadence[k % 4]) for k in range(count)]
    return rewrite_field_flags(data, flags, rate_code=4)


def _interlaced_es(path: Path, seconds: float, offset: int) -> bytes:
    video = (f"nullsrc=duration={seconds}:size=320x240:rate=60000/1001,format=yuv420p,"
             f"{_pattern(offset)},tinterlace=interleave_top,setfield=tff")
    return _encode_es(path, video, ["-c:v", "mpeg2video", "-flags", "+ilme+ildct",
                                    "-bf", "2", "-g", "15", "-b:v", "3M"])


def _mux_es(es: bytes, path: Path, seconds: float) -> Path:
    es_path = path.with_suffix(".m2v")
    es_path.write_bytes(es)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts", "-f", "mpegvideo", "-i", str(es_path),
         "-f", "lavfi", "-i", f"anoisesrc=d={seconds + 1}:r=48000:seed=4",
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "ac3", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def make_soft_telecine_source(path: Path, seconds: float = 4) -> Path:
    """MPEG-2のソフトテレシネ素材(アニメ放送と同じ構造)。"""
    return _mux_es(_soft_telecine_es(path.with_suffix(".st.m2v"), seconds, 0), path, seconds)


def make_mixed_scan_source(path: Path) -> Path:
    """インターレース2秒 → ソフトテレシネ2秒 → インターレース2秒のMPEG-2素材。
    CS-Aの実録画では本編中でこの切り替わりが頻繁に起きていた。"""
    es = (_interlaced_es(path.with_suffix(".a.m2v"), 2, 0)
          + _soft_telecine_es(path.with_suffix(".b.m2v"), 2, 300)
          + _interlaced_es(path.with_suffix(".c.m2v"), 2, 600))
    return _mux_es(es, path, 6)


def frame_scan_flags(path: Path) -> List[tuple]:
    """各フレームの(表示時刻, (interlaced_frame, top_field_first, repeat_pict))。"""
    data = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts_time,interlaced_frame,top_field_first,repeat_pict", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout)
    return [
        (float(f["pts_time"]), (f.get("interlaced_frame", 0), f.get("top_field_first", 0), f.get("repeat_pict", 0)))
        for f in data["frames"] if "pts_time" in f
    ]


def display_field_sources(source: Path, output: Path, cuts, fps: float) -> tuple:
    """(期待, 実際)の「表示されるフィールドごとの元のフレーム番号」の列。

    期待: 残したフレームが、それぞれの表示指示どおりのフィールド数(2または3)で
    順に並ぶ。実際: 出力をrepeatfields(繰り返し指示の展開)+separatefieldsで
    表示フィールド列にし、各フィールドを元の全フィールドと画像で照合する。"""
    def fields(path: Path, vf: str) -> np.ndarray:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
             "-vf", f"{vf},scale=64:ih:flags=area,format=gray", "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout
        return np.frombuffer(raw, np.uint8).reshape(-1, 120, 64).astype(np.float32)

    meta = frame_scan_flags(source)
    video_start = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
         "-of", "json", str(source)], capture_output=True, text=True, check=True,
    ).stdout)["streams"][0]["start_time"]
    video_start = float(video_start)
    keep, cursor = [], 0
    for a, b in sorted(cuts):
        if a > cursor:
            keep.append((cursor, a))
        cursor = max(cursor, b)
    keep.append((cursor, 10 ** 9))

    def kept(t: float) -> bool:
        return any(video_start + (s - 0.5) / fps <= t < video_start + (e - 0.5) / fps for s, e in keep)

    # 元の表示フィールド列と、各フィールドがどのフレームのものか
    source_fields = fields(source, "repeatfields,separatefields")
    owner = []
    for index, (_, (_, _, repeat)) in enumerate(meta):
        owner += [index] * (3 if repeat else 2)
    expected = [owner[k] for k in range(len(owner)) if kept(meta[owner[k]][0])]

    out_fields = fields(output, "repeatfields,separatefields")
    actual = [owner[int(np.argmin(((source_fields - f) ** 2).mean(axis=(1, 2))))] for f in out_fields]
    return expected, actual
