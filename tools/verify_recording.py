"""実際の録画でSmart Cut等を実行し、出力が正しいかを検証する(Windows/Linux共通)。

使い方(プロジェクトのルートで):
    python tools/verify_recording.py 録画.ts --cuts 0-610,20987-23683,54123-54893,89610-90136
    python tools/verify_recording.py 録画.ts --cuts ... --mode full_reencode --out-dir D:\\verify

--cutsは削除する区間(アプリの「残す区間」の補集合。エンジンを直接呼ぶため)(フレーム番号、29.97fps基準の「開始-終了」をカンマ区切り)。
出力はout-dir(既定: .realtest/)に書く。数GBになるので空き容量に注意。

検証項目(すべて合格なら終了コード0):
1. 各フレームの走査情報(インターレースか / トップフィールドが先か / フィールド
   繰り返し)が、元の残したフレームと完全に一致する
2. 映像の中身: 出力の各フレームが、元の対応するフレームと画像として一致する
3. 表示時刻の間隔が2または3フィールドだけ(空き・重なりがない)
4. デコード時刻: 順に増え、表示時刻以下で、1フィールド未満に詰まった箇所がない
5. 音声: カット区間の音声の混入・重複がなく、映像とのずれが1パケット程度以内

検証の考え方はtests/_media_check.pyと同じ(CLAUDE.mdの"Cut accuracy rules"参照)。
実際のアニメには同じ絵のコマや無音が多いため、映像は「期待したフレームとの
差」で、音声は中身が一意なパケットだけで照合する。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ts_smartcut.core.hardware_encoders import get_hardware_encoders  # noqa: E402
from ts_smartcut.ffmpeg.smart_cut import SmartCutEngine  # noqa: E402
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper  # noqa: E402
from ts_smartcut.models import AppSettings, CutRange  # noqa: E402
from ts_smartcut.system.temp_file_manager import TempFileManager  # noqa: E402

W, H = 48, 36
PTS_RE = re.compile(rb"pts_time:\s*([-\d.]+)")
FRAME_MISMATCH_MSE = 30.0
AUDIO_SYNC_LIMIT = 0.035  # AACの1パケット(21ms)+ソフトテレシネの丸め程度


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, **kwargs)


def decode_thumbnails(path: Path):
    """全フレームを小さなグレー画像(uint8)と表示時刻にする。"""
    proc = run(["ffmpeg", "-v", "info", "-nostats", "-copyts", "-i", str(path), "-map", "0:v:0",
                "-fps_mode", "passthrough", "-vf", f"scale={W}:{H}:flags=area,format=gray,showinfo",
                "-f", "rawvideo", "-"])
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, H, W)
    pts = np.array([float(m) for m in PTS_RE.findall(proc.stderr)])
    if len(pts) != len(frames):
        raise RuntimeError(f"フレーム数と時刻の数が一致しません: {len(frames)} / {len(pts)}")
    return frames, pts


def scan_flags(path: Path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "frame=pts_time,interlaced_frame,top_field_first,repeat_pict", "-of", "json", str(path)],
              text=True, encoding="utf-8", errors="replace").stdout
    return [(float(f["pts_time"]), (f.get("interlaced_frame", 0), f.get("top_field_first", 0), f.get("repeat_pict", 0)))
            for f in json.loads(out)["frames"] if "pts_time" in f]


def packet_times(path: Path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "packet=pts_time,dts_time", "-of", "csv=p=0", str(path)],
              text=True, encoding="utf-8", errors="replace").stdout
    rows = [line.strip().rstrip(",").split(",") for line in out.splitlines() if line.strip()]
    return [(float(p), float(d)) for p, d in rows if p != "N/A" and d != "N/A"]


def audio_packets(path: Path):
    """(表示時刻, パケットの中身のmd5)。AACはTSへ書き直すとADTSヘッダーが再生成
    されるため、ヘッダーを外して(aac_adtstoasc)からハッシュする。"""
    # TSではffprobeが同じストリームをprogramsとstreamsの両方に出すことがあるので
    # 最初の行だけを見る
    lines = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                 "-of", "csv=p=0", str(path)], text=True).stdout.split()
    codec = lines[0].rstrip(",") if lines else ""
    bsf = ["-bsf:a", "aac_adtstoasc"] if codec == "aac" else []
    out = run(["ffmpeg", "-v", "error", "-copyts", "-i", str(path), "-map", "0:a:0", "-c", "copy", *bsf,
               "-f", "framemd5", "-"], text=True, encoding="utf-8", errors="replace").stdout
    tb, packets = 1 / 90000, []
    for line in out.splitlines():
        if line.startswith("#tb"):
            num, den = line.split(":")[1].strip().split("/")
            tb = int(num) / int(den)
        elif line and not line.startswith("#"):
            fields = [x.strip() for x in line.split(",")]
            packets.append((int(fields[2]) * tb, fields[5]))
    return packets


def parse_cuts(text: str):
    cuts = []
    for item in text.split(","):
        start, end = item.split("-")
        cuts.append(CutRange(int(start), int(end)))
    return sorted(cuts, key=lambda c: c.start_frame)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("--cuts", required=True)
    parser.add_argument("--mode", default="smart_cut", choices=["smart_cut", "simple_copy", "full_reencode"])
    parser.add_argument("--out-dir", type=Path, default=ROOT / ".realtest")
    args = parser.parse_args()

    source: Path = args.source.resolve()
    cuts = parse_cuts(args.cuts)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    output = args.out_dir / f"{source.stem}_{args.mode}{source.suffix}"

    settings = AppSettings()
    ffmpeg = FFmpegWrapper(settings)
    hardware = get_hardware_encoders(settings)
    print("ハードウェアエンコーダー:\n  " + hardware.describe().replace("\n", "\n  "))

    file_info = ffmpeg.get_file_info(source)
    engine = SmartCutEngine(ffmpeg, TempFileManager(args.out_dir / ".tmp"), settings, hardware)
    started = time.monotonic()
    mode, warning = engine.process(file_info, cuts, output, mode=args.mode)
    print(f"処理: {mode} {time.monotonic() - started:.0f}秒 出力 {output.stat().st_size / 1e9:.2f}GB {output}")
    if warning:
        print(f"警告: {warning}")
    if args.mode == "simple_copy":
        print("Simple Copyはキーフレーム位置へずれる仕様のため、フレーム単位の検証は行いません。")
        return 0

    fps = file_info.fps
    video_start = ffmpeg.get_source_timing(source).video_start
    keep, cursor = [], 0
    for cut in cuts:
        if cut.start_frame > cursor:
            keep.append((cursor, cut.start_frame))
        cursor = max(cursor, cut.end_frame)
    keep.append((cursor, 10 ** 9))
    bounds = [(video_start + (s - 0.5) / fps, video_start + (e - 0.5) / fps) for s, e in keep]

    def segment(t: float):
        return next((i for i, (a, b) in enumerate(bounds) if a <= t < b), None)

    results = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append(ok)
        print(f"[{'合格' if ok else '不合格'}] {name}: {detail}")

    # 1. 走査情報
    expected_flags = [f for t, f in scan_flags(source) if segment(t) is not None]
    output_flags = scan_flags(output)
    check("各フレームの走査情報", [f for _, f in output_flags] == expected_flags,
          f"期待{len(expected_flags)}枚 / 出力{len(output_flags)}枚")

    # 3. 表示時刻の間隔
    field = 1 / (2 * fps)
    steps = Counter(round((b[0] - a[0]) / field, 2) for a, b in zip(output_flags, output_flags[1:]))
    check("表示時刻の間隔", set(steps) <= {2.0, 3.0}, f"フィールド数ごとの件数 {dict(steps)}")

    # 4. デコード時刻
    times = packet_times(output)
    dts = [d for _, d in times]
    tight = sum(1 for a, b in zip(dts, dts[1:]) if b - a < field * 0.99)
    ordered = all(b > a for a, b in zip(dts, dts[1:]))
    not_late = all(d <= p + 1e-6 for p, d in times)
    check("デコード時刻", ordered and not_late and tight == 0,
          f"順に増加={ordered} 表示時刻以下={not_late} 1フィールド未満の詰まり={tight}件")

    # 2. 映像の中身
    source_frames, source_pts = decode_thumbnails(source)
    output_frames, output_pts = decode_thumbnails(output)
    expected_index = [i for i, t in enumerate(source_pts) if segment(t) is not None]
    count = min(len(expected_index), len(output_frames))
    mismatched = 0
    for k in range(0, count, 4096):
        a = source_frames[expected_index[k:k + 4096]].astype(np.float32)
        b = output_frames[k:min(k + 4096, count)].astype(np.float32)
        mismatched += int((((a - b) ** 2).mean(axis=(1, 2)) > FRAME_MISMATCH_MSE).sum())
    check("映像の中身", len(expected_index) == len(output_frames) and mismatched == 0,
          f"期待{len(expected_index)}枚 / 出力{len(output_frames)}枚 / 不一致{mismatched}枚")

    # 5. 音声
    offsets, total = [], 0.0
    for s, e in keep:
        offsets.append(total)
        total += (e - s) / fps

    def expected_time(t: float) -> float:
        i = segment(t)
        return offsets[i] + (t - (video_start + keep[i][0] / fps))

    exp_t = np.array([expected_time(source_pts[i]) for i in expected_index[:count]])
    origin = float(np.median(output_pts[:count] - exp_t))
    src_packets = audio_packets(source)
    unique = Counter(h for _, h in src_packets)
    src_audio = {h: t for t, h in src_packets if unique[h] == 1}
    seen, errors, leaked = Counter(), [], 0
    for t_out, h in audio_packets(output):
        t = src_audio.get(h)
        if t is None:
            continue
        seen[t] += 1
        if segment(t) is None:
            if min(min(abs(t - a), abs(t - b)) for a, b in bounds) > 0.03:
                leaked += 1
            continue
        errors.append(t_out - origin - expected_time(t))
    duplicated = sum(1 for c in seen.values() if c > 1)
    sync = float(np.abs(errors).max()) if errors else 0.0
    # 照合できたパケットが少なすぎる場合は検証できていないので不合格にする
    # (照合0件のまま合格になる不具合があった)
    enough = len(seen) >= 0.5 * len(src_audio) * (len(expected_index) / max(len(source_pts), 1))
    check("音声", enough and leaked == 0 and duplicated == 0 and sync < AUDIO_SYNC_LIMIT,
          f"照合{len(seen)}パケット 混入{leaked} 重複{duplicated} ずれ最大{sync * 1000:.1f}ms")

    print("結果: " + ("すべて合格" if all(results) else "不合格あり"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
