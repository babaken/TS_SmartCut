"""AVS/VPY の出力（設計書 §6.9）の試作: L-SMASH Works で読んだフレームが、アプリの区間と同じ絵かを確かめる。

案 A（2026-10-31 決定）: 動画は repeat=0 で読み、L-SMASH のフレーム k は「録画の中で表示の順に k 番目の絵」（デコード
できない先頭の絵も数える）とする。アプリの残す区間 [N1, N2) は、表示時刻が 映像の start_time + (N-0.5)/fps より前の絵の
数（SmartCutEngine._boundary と同じ刻み）で絵の番号 [k1, k2) に直す。

確かめること（どれも 64x36 の輝度に縮めて比べる）:

- 絵の番号: 映像の全パケットの表示時刻を並べた順位を、ffmpeg でデコードした各絵に付け、L-SMASH のフレーム k が順位 k の絵と
  同じ絵か（VPY は vspipe、AVS は ffmpeg の AviSynth 入力。L-SMASH Works は --lsmash の DLL を LoadPlugin する）。
- 区間の境目: アプリのフレーム N の絵（_boundary の刻みで選んだ絵）の順位が、上の式で求めた絵の番号と同じか。
- --repeat1: 前に試した repeat=1（29.97 fps）で、L-SMASH のフレーム n とアプリのフレーム n+d の d を数える（案 A に決めた
  理由の記録。ソフトテレシネでは d が途中で変わる・CS の一部の局は読めない）。

索引（.lwi）は --work の下に作る（録画の横に作らない。録画は読むだけ）。

    python tools/check_script_frames.py --work <作業フォルダ> --lsmash <L-SMASH Works の LSMASHSource.dll> \
        --vspipe <vspipe.exe> <録画.ts> ...
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import numpy as np

W, H = 64, 36
FRAME = W * H
MAX_SHIFT = 6
# 縮小のしかたが違う（ffmpeg と AVS の bilinear）ので、完全一致ではなく差の平均（0〜255）で比べる
SAME = 3.0
# 2 番目に近いずれとの差がこれより小さいフレームは、静止画などで決められないので数えない
MARGIN = 1.5

_PTS = re.compile(r"pts_time:\s*(-?[0-9.]+)")


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=start_time:stream=index,codec_type,codec_name,start_time,r_frame_rate,avg_frame_rate,field_order",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    return json.loads(out.stdout)


def _rate(text: str) -> float:
    num, _, den = text.partition("/")
    return float(num) / float(den or 1) if float(den or 1) else 0.0


def source_info(path: Path) -> dict:
    data = _probe(path)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    r, avg = _rate(video["r_frame_rate"]), _rate(video["avg_frame_rate"])
    # wrapper._video_frame_rate と同じ: r がフィールドレート（ソフトテレシネ）なら avg
    fps = avg if avg > 0 and abs(r - 2 * avg) <= 0.02 else r
    return {"codec": video["codec_name"], "field_order": video.get("field_order", ""),
            "video_start": float(video["start_time"]), "fps": fps,
            "soft_telecine": avg > 0 and abs(r - 2 * avg) <= 0.02}


def _read_raw(cmd: list, stderr=subprocess.DEVNULL, frame_bytes: int = FRAME, stdin=None) -> tuple[np.ndarray, str]:
    proc = subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-2000:] if proc.stderr else ""
        raise RuntimeError(f"failed ({proc.returncode}): {' '.join(map(str, cmd[:3]))} ...\n{err}")
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(data) // frame_bytes
    # yuv420p の時は先頭の輝度の面だけ使う
    pics = data[: n * frame_bytes].reshape(n, frame_bytes)[:, :FRAME].copy()
    return pics, (proc.stderr or b"").decode("utf-8", "replace")


def decoded_pictures(path: Path) -> tuple[np.ndarray, list]:
    """ffmpeg でシークせずにデコードした絵（表示の順）と表示時刻。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-copyts", "-i", str(path), "-map", "0:v:0",
           # gray に変えると輝度の範囲まで変わる（AVS/VS は Y をそのまま縮める）ので yuv420p の Y を使う
           "-vf", f"scale={W}:{H}:flags=bilinear,format=yuv420p,showinfo", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-"]
    pics, log = _read_raw(cmd, stderr=subprocess.PIPE, frame_bytes=FRAME * 3 // 2)
    pts = [float(m.group(1)) for line in log.splitlines() if "Parsed_showinfo" in line
           for m in [_PTS.search(line)] if m]
    if len(pts) != len(pics):
        raise RuntimeError(f"showinfo {len(pts)} != frames {len(pics)}")
    return pics, pts


def packet_times(path: Path) -> list:
    """映像の全パケットの表示時刻（並べ替え済み）。表示時刻の無いパケットは数えない。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    values = [v.strip(",") for v in out.stdout.split()]
    return sorted(float(v) for v in values if v not in ("", "N/A"))


def picture_index(times: list, t: float) -> int:
    """表示時刻が t より前の絵の数 = 表示時刻 t 以降の最初の絵の番号。"""
    return bisect.bisect_left(times, t - 1e-6)


def app_boundary_pictures(times: list, pts: list, info: dict) -> dict:
    """アプリのフレーム N に当たる絵の番号を 2 通りで求めて比べる（式と、デコードした絵を _boundary の刻みで選んだもの）。"""
    vs, fps = info["video_start"], info["fps"]
    total = int(math.floor((pts[-1] - vs) * fps + 0.5)) + 1
    mismatch, j, first_bad = 0, 0, None
    first = picture_index(times, pts[0])
    for n in range(total):
        t = vs + (n - 0.5) / fps
        while j < len(pts) and pts[j] < t - 1e-6:
            j += 1
        if j >= len(pts):
            break
        by_formula = picture_index(times, t)
        by_decode = picture_index(times, pts[j])
        # デコードできない先頭の絵の所は、アプリは最初のデコードできる絵を出す（区間は _first_decodable_frame で詰める）
        if by_formula >= first and by_formula != by_decode:
            mismatch += 1
            first_bad = first_bad if first_bad is not None else n
    return {"app_frames": total, "boundary_mismatch": mismatch, "first_mismatch_frame": first_bad}


def vpy_frames(path: Path, work: Path, vspipe: str, repeat: int) -> np.ndarray:
    script = work / f"probe_{_key(path)}_r{repeat}.vpy"
    script.write_text(
        "import vapoursynth as vs\ncore = vs.core\n"
        f"v = core.lsmas.LWLibavSource({str(path)!r}, repeat={repeat}, cachedir={str(work / 'cache')!r})\n"
        "v.set_output(0)\n", encoding="utf-8")
    # VapourSynth の縮小は ffmpeg と結果が違うので、元の大きさで受け取り、アプリ側と同じ ffmpeg の縮小にかける
    # （同じ縮小なら、デコードした絵とビット単位で同じになる）
    vsp = subprocess.Popen([vspipe, "-c", "y4m", str(script), "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        pics, _ = _read_raw(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "yuv4mpegpipe", "-i", "-",
                             "-vf", f"scale={W}:{H}:flags=bilinear,format=yuv420p", "-f", "rawvideo", "-"],
                            frame_bytes=FRAME * 3 // 2, stdin=vsp.stdout)
    finally:
        vsp.stdout.close()
        if vsp.wait() != 0:
            raise RuntimeError(f"failed ({vsp.returncode}): {vspipe} {script}")
    return pics


def avs_frames(path: Path, work: Path, lsmash: Path, repeat: bool) -> np.ndarray:
    script = work / f"probe_{_key(path)}_r{int(repeat)}.avs"
    script.write_text(
        f'LoadPlugin("{lsmash}")\n'
        f'LWLibavVideoSource("{path}", repeat={str(repeat).lower()}, cachedir="{work / "cache"}")\n'
        f"BilinearResize({W}, {H}).ConvertToY8()\n", encoding="utf-8")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-f", "avisynth", "-i", str(script),
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return _read_raw(cmd)[0]


def _key(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]


def _diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b).mean())


def compare_pictures(pics: np.ndarray, ranks: list, other: np.ndarray) -> dict:
    """L-SMASH のフレーム k と、順位 k のデコードした絵を比べる。合わない時は近くの絵とのずれを数える。"""
    by_rank = {r: j for j, r in enumerate(ranks)}
    same = 0
    shifts: Counter = Counter()
    unknown = 0
    for k in range(len(other)):
        j = by_rank.get(k)
        if j is None:
            continue  # デコードできない先頭の絵（L-SMASH は近くの絵で埋める）
        if _diff(other[k], pics[j]) <= SAME:
            same += 1
            continue
        near = sorted((_diff(other[k], pics[i]), i - j)
                      for i in range(max(0, j - MAX_SHIFT), min(len(pics), j + MAX_SHIFT + 1)))
        if near[0][0] <= SAME:
            shifts[near[0][1]] += 1
        else:
            unknown += 1
    return {"lsmash_frames": len(other), "compared": len(by_rank), "same": same,
            "shifted": dict(sorted(shifts.items())), "no_match": unknown}


def repeat1_shifts(app: np.ndarray, other: np.ndarray) -> dict:
    """repeat=1 の L-SMASH のフレーム n に一番近いアプリのフレーム n+d の d（10 に分けた部分ごとにも）。"""
    def count(frames) -> Counter:
        shifts: Counter = Counter()
        for n in frames:
            diffs = sorted((_diff(other[n], app[n + d]), d)
                           for d in range(-MAX_SHIFT, MAX_SHIFT + 1) if 0 <= n + d < len(app))
            if diffs[0][0] <= SAME and (len(diffs) == 1 or diffs[1][0] - diffs[0][0] >= MARGIN):
                shifts[diffs[0][1]] += 1
        return shifts

    size = max(1, len(other) // 10)
    chunks = [count(range(k * size, min(len(other), (k + 1) * size))).most_common(1) for k in range(10)]
    return {"frames": len(other), "app_frames": len(app), "shifts": dict(sorted(count(range(len(other))).items())),
            "chunks": [c[0][0] if c else None for c in chunks]}


def app_frames(pics: np.ndarray, pts: list, info: dict) -> np.ndarray:
    """アプリのフレーム番号順の絵（_boundary の刻みで選ぶ）。"""
    vs, fps = info["video_start"], info["fps"]
    total = int(math.floor((pts[-1] - vs) * fps + 0.5)) + 1
    index, j = [], 0
    for n in range(total):
        t = vs + (n - 0.5) / fps
        while j < len(pts) and pts[j] < t - 1e-6:
            j += 1
        index.append(min(j, len(pts) - 1))
    return pics[index]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recordings", nargs="+", type=Path)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--lsmash", type=Path, required=True)
    ap.add_argument("--vspipe", required=True)
    ap.add_argument("--no-avs", action="store_true")
    ap.add_argument("--repeat1", action="store_true", help="repeat=1 のずれも数える（記録用）")
    args = ap.parse_args(argv)
    # スクリプトは --work に書くので、中に書くパスは絶対パスにする（相対パスでは録画が見つからない）
    args.recordings = [p.resolve() for p in args.recordings]
    args.work = args.work.resolve()
    args.lsmash = args.lsmash.resolve()
    (args.work / "cache").mkdir(parents=True, exist_ok=True)
    for i, path in enumerate(args.recordings):
        info = source_info(path)
        pics, pts = decoded_pictures(path)
        times = packet_times(path)
        ranks = [picture_index(times, t) for t in pts]
        result = {"recording": i, **{k: info[k] for k in ("codec", "field_order", "soft_telecine")},
                  "packets": len(times), "decoded": len(pts), "leading_undecodable": ranks[0],
                  **app_boundary_pictures(times, pts, info)}
        variants = [("vpy", lambda r: vpy_frames(path, args.work, args.vspipe, r))]
        if not args.no_avs:
            variants.append(("avs", lambda r: avs_frames(path, args.work, args.lsmash, bool(r))))
        for name, read in variants:
            try:
                result[f"{name}_repeat0"] = compare_pictures(pics, ranks, read(0))
            except RuntimeError as e:
                result[f"{name}_repeat0"] = {"error": str(e).splitlines()[-1][-300:]}
            if args.repeat1:
                try:
                    result[f"{name}_repeat1"] = repeat1_shifts(app_frames(pics, pts, info), read(1))
                except RuntimeError as e:
                    result[f"{name}_repeat1"] = {"error": str(e).splitlines()[-1][-300:]}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
