"""特徴の計算の速さの内訳を測る (速さの改善の基準は遅い PC)。

録画ごとに、条件 A(残した区間あり: 録画の 10〜45%・55〜90%)と B(なし。A で作った型を局の型として渡す)で
features.compute_features を動かし、次を測る。特徴の値は変えない(測るだけ)。

1. 計算の内訳: 全体、ffprobe、読み込み(Rust)、読み終わった後の Python の計算(うちロゴの一致度・
   ロゴ画像の材料)と、読み込みの中の待ち時間(ts_smartcut_native の finish() の timing)
2. 計算の中の ffmpeg ごと(灰色のフレーム用・ロゴ画像の材料用)の実時間・CPU 時間・平均の並列度・使った CPU の数・
   CPU を 1 秒以上使ったスレッドの数(Linux の /proc を 0.2 秒ごとに見る。Linux 以外は psutil があれば CPU 時間だけ)
3. ffmpeg 単独(録画を直接読む): 灰色のフレーム用だけ・ロゴ画像の材料用だけ・2 つ同時・映像の取り出しだけ(復号なし)。
   2 つの ffmpeg が同じ映像を 2 回復号している分の損の見積もりに使う
4. 灰色のフレーム用の ffmpeg のスレッド数(-threads)を変えた時の時間と、出力(フレーム・時刻・音声)が
   既定と同じか(出力が同じなら特徴の値も同じ)
5. (H.264 の録画だけ、--combined)2 つの ffmpeg を 1 回の復号にまとめた時の時間と、出力が同じか
6. 灰色のフレーム用のフィルターを「先に灰色にしてから縮める」順にした時の時間と、出力が同じか

結果は <出力フォルダ>/feature_speed.json と、PR のコメントに貼れる feature_speed.md。録画の名前は出さず、
--recording に付けた記号(GR・BS・CS)だけを書く。

使い方:
    python tools/measure_feature_speed.py --recording GR=<録画.ts> --recording BS=<録画.ts> --recording CS=<録画.ts>
        [--out <出力フォルダ>] [--repeat 2] [--threads 0,1,2,4,8,0] [--combined] [--skip-alone]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from ts_smartcut.autocut import features  # noqa: E402
from ts_smartcut.autocut.features import KeepRange, compute_features  # noqa: E402
from ts_smartcut.autocut.logo import LogoTemplate  # noqa: E402
from ts_smartcut.autocut.native import native_module  # noqa: E402
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper  # noqa: E402
from ts_smartcut.models import AppSettings  # noqa: E402

PROC = Path("/proc")
_REAL_POPEN = subprocess.Popen


# ---- 子プロセスの CPU 時間 -----------------------------------------------------------------


class ProcessWatch:
    """子プロセスの CPU 時間を 0.2 秒ごとに見る(Linux は /proc、ほかは psutil があれば)。"""

    INTERVAL = 0.2

    def __init__(self):
        self.entries: List[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") and PROC.exists() else None
        try:
            import psutil  # noqa: F401
            self._psutil = True
        except ImportError:
            self._psutil = False

    def start(self) -> "ProcessWatch":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._sample()
        self._stop.set()
        self._thread.join()

    def watch(self, process, kind: str) -> dict:
        entry = {"kind": kind, "pid": process.pid, "process": process, "started": time.monotonic(), "ended": None,
                 "cpu": 0.0, "cpus": set(), "threads": {}, "max_threads": 0}
        with self._lock:
            self.entries.append(entry)
        return entry

    def _run(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            self._sample()

    def _sample(self) -> None:
        with self._lock:
            entries = [e for e in self.entries if e["ended"] is None]
        for e in entries:
            if self._sample_one(e) is False or e["process"].poll() is not None:
                e["ended"] = e["ended"] or time.monotonic()

    def _sample_one(self, e: dict) -> Optional[bool]:
        """見られたら True、終わっていたら False、この OS で見る方法が無ければ None。"""
        pid = e["pid"]
        if self._tick is not None:
            try:
                fields = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z":
                    return False
                e["cpu"] = (int(fields[11]) + int(fields[12])) / self._tick
                e["max_threads"] = max(e["max_threads"], int(fields[17]))
                for task in (PROC / str(pid) / "task").iterdir():
                    try:
                        t = (task / "stat").read_text().rsplit(")", 1)[1].split()
                    except OSError:
                        continue
                    e["threads"][task.name] = (int(t[11]) + int(t[12])) / self._tick
                    e["cpus"].add(int(t[36]))
                return True
            except (OSError, IndexError, ValueError):
                return False
        if self._psutil:
            import psutil
            try:
                p = psutil.Process(pid)
                times = p.cpu_times()
                e["cpu"] = times.user + times.system
                e["max_threads"] = max(e["max_threads"], p.num_threads())
                return True
            except psutil.Error:
                return False
        return None

    @staticmethod
    def summary(e: dict) -> dict:
        wall = (e["ended"] or time.monotonic()) - e["started"]
        seen = e["max_threads"] > 0  # CPU 時間を見られた(Linux 以外で psutil が無いと見られない)
        return {
            "kind": e["kind"], "wall": round(wall, 2), "cpu": round(e["cpu"], 2) if seen else None,
            "parallel": round(e["cpu"] / wall, 2) if seen and wall > 0 else None,
            "cpus_used": len(e["cpus"]) or None,
            "busy_threads": sum(1 for v in e["threads"].values() if v >= 1.0) or None,
            "max_threads": e["max_threads"] or None,
        }


def kind_of(args) -> str:
    text = " ".join(str(a) for a in args)
    if "ffprobe" in text:
        return "ffprobe"
    if "-skip_frame" in text:
        return "image"
    if "format=gray" in text:
        return "frames"
    return "other"


class RecordProcesses:
    """この間に作った子プロセス(subprocess.Popen)を ProcessWatch に渡し、コマンドを覚える。"""

    def __init__(self, watch: ProcessWatch):
        self.watch = watch
        self.commands: Dict[str, List[str]] = {}

    def __enter__(self):
        recorder = self

        class Recorded(_REAL_POPEN):
            def __init__(self, args, *a, **k):
                super().__init__(args, *a, **k)
                kind = kind_of(args)
                recorder.commands.setdefault(kind, [str(x) for x in args])
                recorder.watch.watch(self, kind)

        subprocess.Popen = Recorded
        return self

    def __exit__(self, *exc):
        subprocess.Popen = _REAL_POPEN


# ---- 計算の内訳 ---------------------------------------------------------------------------


class Breakdown:
    """compute_features の中の ffprobe・読み込みの時間と、Rust の読み込みの内訳を覚える。"""

    def __init__(self):
        self.probe = 0.0
        self.reading = 0.0
        self.native_timing: Optional[dict] = None
        self._saved = {}

    def __enter__(self):
        b = self
        for name in ("_probe", "_read_native"):
            original = getattr(features, name)
            self._saved[name] = original

            def timed(*a, _original=original, _name=name, **k):
                started = time.monotonic()
                try:
                    return _original(*a, **k)
                finally:
                    if _name == "_probe":
                        b.probe += time.monotonic() - started
                    else:
                        b.reading += time.monotonic() - started

            setattr(features, name, timed)
        native = native_module()
        real = native.FeatureReader
        self._saved["FeatureReader"] = (native, real)

        class Reader:
            def __init__(self, *a, **k):
                self.reader = real(*a, **k)

            def read_frames(self, tick, alive):
                return self.reader.read_frames(tick, alive)

            def finish(self):
                result = self.reader.finish()
                b.native_timing = dict(result.get("timing") or {})
                return result

        native.FeatureReader = Reader
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if name == "FeatureReader":
                module, real = value
                module.FeatureReader = real
            else:
                setattr(features, name, value)


def run_features(path: Path, fps: float, keep, station) -> dict:
    watch = ProcessWatch().start()
    with RecordProcesses(watch) as recorded, Breakdown() as b:
        started = time.monotonic()
        f = compute_features(path, fps, keep, station_template=station)
        total = time.monotonic() - started
    watch.stop()
    timing = f.timing or {}
    return {
        "features": f,
        "commands": recorded.commands,
        "result": {
            "complete": f.complete, "total": round(total, 2),
            "probe": round(b.probe, 2), "reading": round(b.reading, 2),
            "after_reading": round(total - b.probe - b.reading, 2),
            "logo": timing.get("logo"), "logo_image": timing.get("logo_image"),
            "native_timing": {k: round(v, 2) for k, v in (b.native_timing or {}).items()},
            "processes": [ProcessWatch.summary(e) for e in watch.entries if e["kind"] in ("frames", "image")],
        },
    }


def station_from(template: LogoTemplate) -> LogoTemplate:
    """条件 B の局の型(A の型を float16 にしたもの。局の型は DB に float16 で残る)。"""
    t = template
    return LogoTemplate(t.gx.astype(np.float16).astype(np.float32), t.gy.astype(np.float16).astype(np.float32),
                        t.mask_x, t.mask_y, t.strength, t.frames)


def same_features(a, b) -> bool:
    for name in ("scene_change", "audio_level", "corner_kept_mean", "corner_kept_std", "corner_other_mean",
                 "corner_other_std", "logo_score"):
        if not np.array_equal(np.asarray(getattr(a, name)), np.asarray(getattr(b, name)), equal_nan=True):
            return False
    return a.frame_count == b.frame_count


# ---- ffmpeg 単独 --------------------------------------------------------------------------


def alone_command(cmd: List[str], path: Path, work: Path, tag: str, threads: Optional[int] = None) -> List[str]:
    """計算の中の ffmpeg のコマンドを、録画を直接読み、一時ファイルを work に書くものにする。"""
    out = list(cmd)
    i = out.index("pipe:0")
    out[i] = str(path)
    for option in ("-stats_mux_pre",):
        if option in out:
            out[out.index(option) + 1] = str(work / f"{tag}.stats.txt")
    for k, arg in enumerate(out):
        if arg.endswith("audio.s16"):
            out[k] = str(work / f"{tag}.audio.s16")
    if threads:
        f = max(k for k in range(i) if out[k] == "-f")  # 入力の -f mpegts の前に
        out[f:f] = ["-threads", str(threads)]
    return out


def run_alone(commands: Dict[str, List[str]], watch: ProcessWatch) -> Dict[str, dict]:
    """commands(名前 → コマンド)を同時に動かし、それぞれの実時間・CPU・出力のハッシュを返す。"""
    running = {}
    for name, cmd in commands.items():
        p = _REAL_POPEN(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        entry = watch.watch(p, name)
        digest = hashlib.sha256()
        errors: List[bytes] = []

        def drain(p=p, digest=digest):
            for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
                digest.update(chunk)

        threads = [threading.Thread(target=drain, daemon=True),
                   threading.Thread(target=lambda p=p, errors=errors: errors.append(p.stderr.read()), daemon=True)]
        for t in threads:
            t.start()
        running[name] = (p, entry, digest, threads, errors, cmd)
    result = {}
    for name, (p, entry, digest, threads, errors, cmd) in running.items():
        p.wait()
        entry["ended"] = entry["ended"] or time.monotonic()
        for t in threads:
            t.join()
        files = hashlib.sha256()
        for k, arg in enumerate(cmd):
            if arg.endswith((".stats.txt", ".audio.s16")) and Path(arg).exists():
                files.update(Path(arg).read_bytes())
        for k, arg in enumerate(cmd):
            if arg.endswith((".stats.txt", ".audio.s16")):
                Path(arg).unlink(missing_ok=True)  # 次に同じ名前で書く時に上書きを尋ねられないように
        result[name] = {**ProcessWatch.summary(entry), "returncode": p.returncode,
                        "output": digest.hexdigest()[:16], "files": files.hexdigest()[:16],
                        "error": b"".join(errors).decode("utf-8", "replace")[-300:] if p.returncode else ""}
    return result


def alone(commands: Dict[str, List[str]]) -> Dict[str, dict]:
    watch = ProcessWatch().start()
    try:
        return run_alone(commands, watch)
    finally:
        watch.stop()


def combined_command(frames_cmd: List[str], image_cmd: List[str], path: Path, work: Path) -> Optional[List[str]]:
    """2 つの ffmpeg を 1 回の復号にまとめたコマンド(H.264 だけ。灰色のフレーム用が -lowres を使う MPEG-2 は
    復号が違うのでまとめられない)。ロゴ画像の材料は select でキーフレームだけにする。"""
    if "-lowres" in frames_cmd:
        return None
    vf_frames = frames_cmd[frames_cmd.index("-vf") + 1]
    vf_image = image_cmd[image_cmd.index("-vf") + 1]
    i = frames_cmd.index("-i")
    prefix = [a for a in frames_cmd[:i - 2]]  # -f mpegts の前まで
    graph = f"[0:v:0]split=2[a][b];[a]{vf_frames}[fo];[b]select='eq(key\\,1)',{vf_image}[io]"
    cmd = prefix + ["-f", "mpegts", "-i", str(path), "-filter_complex", graph,
                    "-map", "[fo]", "-fps_mode", "passthrough", "-enc_time_base", "demux", "-f", "rawvideo",
                    "-stats_mux_pre", str(work / "combined.frames.stats.txt"), "-stats_mux_pre_fmt", "{ptsi} {tb}",
                    str(work / "combined.frames.raw"),
                    "-map", "[io]", "-fps_mode", "passthrough", "-enc_time_base", "1/90000", "-f", "rawvideo",
                    "-stats_mux_pre", str(work / "combined.image.stats.txt"), "-stats_mux_pre_fmt", "{pts} {tb}",
                    str(work / "combined.image.raw")]
    return cmd


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# ---- 表 -----------------------------------------------------------------------------------


def machine() -> dict:
    info = {"os": platform.platform(terse=True), "python": platform.python_version(), "numpy": np.__version__,
            "cpus": os.cpu_count()}
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, errors="replace").stdout
        info["ffmpeg"] = out.split("\n", 1)[0].replace("ffmpeg version ", "").split(" Copyright")[0]
    except OSError:
        pass
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if governor.exists():
        info["governor"] = governor.read_text().strip()
    info["native"] = native_module().VERSION
    return info


def markdown(report: dict) -> str:
    m = report["machine"]
    lines = [f"## 特徴の計算の速さ（{time.strftime('%Y-%m-%d')}）", "",
             f"OS {m['os']}・論理 CPU {m['cpus']}・Python {m['python']}・numpy {m['numpy']}・ffmpeg {m.get('ffmpeg')}・"
             f"CPU の周波数の設定 {m.get('governor', '不明')}・ts_smartcut_native {m.get('native')}", ""]
    recs = report["recordings"]
    lines += ["### 1. 計算の内訳（秒、各 %d 回の平均）" % report["repeat"], "",
              "| 録画 | 条件 | 全体 | ffprobe | 読み込み | 読んだ後の Python | うちロゴの一致度 | うちロゴ画像の材料 |",
              "|---|---|---|---|---|---|---|---|"]
    for r in recs:
        for cond in ("A", "B"):
            for run in r["runs"].get(cond, []):
                lines.append(f"| {r['label']} | {cond} | {run['total']} | {run['probe']} | "
                             f"{run['reading']} | {run['after_reading']} | {run['logo']} | {run['logo_image']} |")
    names = ["frames_total", "frames_wait_ffmpeg", "frames_wait_stats", "frames_compute", "frames_write_corners",
             "frames_tick", "feed_total", "feed_read_file", "feed_scan", "feed_video_blocked",
             "feed_image_queue_blocked", "image_feed_blocked", "image_wait_ffmpeg"]
    lines += ["", "### 2. Rust の読み込みの内訳（秒、各スレッドの合計）", "",
              "| 録画 | 条件 | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for r in recs:
        for cond in ("A", "B"):
            for run in r["runs"].get(cond, []):
                t = run["native_timing"]
                lines.append(f"| {r['label']} | {cond} | " + " | ".join(str(t.get(n, "")) for n in names) + " |")
    lines += ["", "### 3. 計算の中の ffmpeg", "",
              "| 録画 | 条件 | ffmpeg | 実時間 | CPU 秒 | 平均の並列度 | 使った CPU | 1 秒以上のスレッド | 最大スレッド |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        for cond in ("A", "B"):
            for run in r["runs"].get(cond, []):
                for p in run["processes"]:
                    lines.append(f"| {r['label']} | {cond} | {p['kind']} | {p['wall']} | {p['cpu']} | "
                                 f"{p['parallel']} | {p['cpus_used']} | {p['busy_threads']} | {p['max_threads']} |")
    lines += ["", "### 4. ffmpeg 単独（録画を直接読む）", "",
              "| 録画 | 動かしたもの | ffmpeg | 実時間 | CPU 秒 | 平均の並列度 | 使った CPU |", "|---|---|---|---|---|---|---|"]
    for r in recs:
        for name, group in r.get("alone", {}).items():
            for kind, p in group.items():
                lines.append(f"| {r['label']} | {name} | {kind} | {p['wall']} | {p['cpu']} | {p['parallel']} | "
                             f"{p['cpus_used']} |")
    lines += ["", "### 5. 灰色のフレーム用の ffmpeg のスレッド数（単独）", "",
              "| 録画 | -threads | 実時間 | CPU 秒 | 平均の並列度 | 出力が既定と同じ |", "|---|---|---|---|---|---|"]
    for r in recs:
        for row in r.get("threads", []):
            lines.append(f"| {r['label']} | {row['threads'] or '既定'} | {row['wall']} | {row['cpu']} | "
                         f"{row['parallel']} | {row['same']} |")
    combined = [r for r in recs if r.get("combined")]
    if combined:
        lines += ["", "### 6. 2 つの ffmpeg を 1 回の復号にまとめた試し（H.264 だけ）", "",
                  "| 録画 | 実時間 | CPU 秒 | 別々の 2 つ同時の実時間 | 灰色の出力が同じ | ロゴ画像の出力が同じ |",
                  "|---|---|---|---|---|---|"]
        for r in combined:
            c = r["combined"]
            lines.append(f"| {r['label']} | {c['wall']} | {c['cpu']} | {c['separate_wall']} | {c['frames_same']} | "
                         f"{c['image_same']} |")
    gray = [r for r in recs if r.get("gray_first")]
    if gray:
        lines += ["", "### 7. 灰色のフレーム用のフィルターの順（先に灰色にしてから縮める、単独）", "",
                  "| 録画 | フィルター | 実時間 | CPU 秒 | 既定の順の実時間 | 出力が同じ |", "|---|---|---|---|---|---|"]
        for r in gray:
            g = r["gray_first"]
            lines.append(f"| {r['label']} | `{g['filter']}` | {g['wall']} | {g['cpu']} | {g['default_wall']} | {g['same']} |")
    lines += ["", "### 8. 録画", "", "| 録画 | 長さ（分） | 大きさ（GB） | 映像 | 大きさ（画素） |", "|---|---|---|---|---|"]
    for r in recs:
        lines.append(f"| {r['label']} | {r['minutes']} | {r['gigabytes']} | {r['codec']} | {r['size']} |")
    return "\n".join(lines) + "\n"


# ---- 本体 ---------------------------------------------------------------------------------


def measure_recording(label: str, path: Path, args, work: Path) -> dict:
    info = FFmpegWrapper(AppSettings()).get_file_info(path)
    n = int(info.duration * info.fps)
    keep = [KeepRange(int(n * .10), int(n * .45)), KeepRange(int(n * .55), int(n * .90))]
    probe = features._probe(path, "ffprobe")
    record = {"label": label, "minutes": round(info.duration / 60, 1), "gigabytes": round(path.stat().st_size / 2**30, 2),
              "codec": probe.codec, "size": f"{probe.width}x{probe.height}", "runs": {"A": [], "B": []}}
    commands: Dict[str, List[str]] = {}
    station = None
    for k in range(args.repeat):
        print(f"[{label}] 条件 A {k + 1}/{args.repeat}", flush=True)
        a = run_features(path, info.fps, keep, None)
        record["runs"]["A"].append(a["result"])
        commands.update({k2: v for k2, v in a["commands"].items() if k2 not in commands})
        if station is None:
            station = station_from(a["features"].logo_template)
        print(f"[{label}] 条件 B {k + 1}/{args.repeat}", flush=True)
        b = run_features(path, info.fps, [], station)
        record["runs"]["B"].append(b["result"])
    if args.skip_alone or "frames" not in commands:
        return record

    frames_cmd = commands["frames"]
    image_cmd = commands.get("image")
    demux = [*features.launch_prefix(), "ffmpeg", "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0",
             "-c", "copy", "-f", "null", "-"]
    groups = {"灰色のフレーム用だけ": {"frames": alone_command(frames_cmd, path, work, "frames")},
              "映像の取り出しだけ（復号なし）": {"demux": alone_command(demux, path, work, "demux")}}
    if image_cmd:
        groups["ロゴ画像の材料用だけ"] = {"image": alone_command(image_cmd, path, work, "image")}
        groups["2 つ同時"] = {"frames": alone_command(frames_cmd, path, work, "frames2"),
                             "image": alone_command(image_cmd, path, work, "image2")}
    record["alone"] = {}
    for name, cmds in groups.items():
        print(f"[{label}] ffmpeg 単独: {name}", flush=True)
        record["alone"][name] = alone(cmds)

    record["threads"] = []
    base = None
    for threads in args.threads:
        print(f"[{label}] スレッド数 {threads or '既定'}", flush=True)
        r = alone({"frames": alone_command(frames_cmd, path, work, f"t{threads}", threads or None)})["frames"]
        key = (r["output"], r["files"])
        if base is None:
            base = key
        record["threads"].append({"threads": threads, "wall": r["wall"], "cpu": r["cpu"], "parallel": r["parallel"],
                                  "same": key == base})
    print(f"[{label}] フィルターの順(先に灰色)", flush=True)
    gray_first = alone_command(frames_cmd, path, work, "grayfirst")
    k = gray_first.index("-vf") + 1
    scale, _, fmt = gray_first[k].partition(",")
    gray_first[k] = f"{fmt},{scale}"
    r = alone({"frames": gray_first})["frames"]
    default = record["threads"][0] if record["threads"] and record["threads"][0]["threads"] == 0 else None
    record["gray_first"] = {"filter": gray_first[k], "wall": r["wall"], "cpu": r["cpu"],
                            "default_wall": default["wall"] if default else None,
                            "same": base is not None and (r["output"], r["files"]) == base}
    if args.combined and image_cmd:
        cmd = combined_command(frames_cmd, image_cmd, path, work)
        if cmd is not None:
            print(f"[{label}] 1 回の復号にまとめた試し", flush=True)
            separate = record["alone"].get("2 つ同時", {})
            r = alone({"combined": cmd})["combined"]
            frames_ref = alone({"frames": alone_command(frames_cmd, path, work, "ref")})["frames"]
            image_ref = alone({"image": alone_command(image_cmd, path, work, "refi")})["image"]
            record["combined"] = {
                "wall": r["wall"], "cpu": r["cpu"],
                "separate_wall": max((p["wall"] for p in separate.values()), default=None),
                "frames_same": file_hash(work / "combined.frames.raw") == frames_ref["output"],
                "image_same": file_hash(work / "combined.image.raw") == image_ref["output"],
            }
            for name in ("combined.frames.raw", "combined.image.raw"):
                (work / name).unlink(missing_ok=True)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recording", action="append", required=True, help="記号=録画のパス(GR=/path/a.ts)")
    parser.add_argument("--out", type=Path, default=Path("speed_results"))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--threads", default="0,1,2,4,8,0",
                        help="試す -threads(0 = 既定。最後の 0 は既定の出力が毎回同じかを見る)")
    parser.add_argument("--combined", action="store_true", help="1 回の復号にまとめた試し(H.264 だけ)")
    parser.add_argument("--skip-alone", action="store_true", help="ffmpeg 単独とスレッド数を測らない")
    args = parser.parse_args()
    args.threads = [int(x) for x in args.threads.split(",") if x != ""]
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg が見つからない")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"machine": machine(), "repeat": args.repeat, "recordings": []}
    for item in args.recording:
        label, _, path = item.partition("=")
        with tempfile.TemporaryDirectory(prefix="ts_smartcut_speed_") as work:
            report["recordings"].append(measure_recording(label, Path(path), args, Path(work)))
        (args.out / "feature_speed.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (args.out / "feature_speed.md").write_text(markdown(report), encoding="utf-8")
    print(f"結果: {args.out / 'feature_speed.md'}")


if __name__ == "__main__":
    main()
