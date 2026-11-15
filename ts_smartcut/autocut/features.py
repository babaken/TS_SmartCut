"""時系列の特徴の計算 (設計書 §31.4、自動カット計画 A3)。

録画1本から、CM の境目とロゴの判定に使う数値を計算する。録画そのものは保存
しないので、録画があるうち(バッチ処理で記録した直後)に計算して DB に残す。

- 音量: 10ms ごとのピーク(core/waveform.py の音声波形と同じ作り方)を dB にする。
- 場面の切り替わりの強さ: 縮小したグレーの映像の、前のフレームとの差の平均。
- 四隅: 画面の四隅を切り出し、「残した」区間と「それ以外」のそれぞれで、画素
  ごとの平均と標準偏差を集計する(ロゴの画像として見られる)。
- ロゴの一致度: 録画自身の残した区間から型を作り、フレームごと・隅ごとの一致度を
  計算する(logo.py)。四隅を 1/8 にして一時ファイルに溜め、読み終わってから計算する。
- 音声の形式・字幕の変化: stream_events.py。
- 局のロゴ画像(.lgd)の材料: 残した区間のキーフレームを、2 つ目の ffmpeg で元の解像度・カラーの
  四隅として読む(logo_image.py)。残した区間が無い時(カットする前)は読まない。

録画は1回だけ読む(計画書 A3「録画を何度も読まない」)。Rust の部品(native/ の ts_smartcut_native、
native.py)が録画を読みながら TS を解析し(音声の形式・字幕)、同じデータを ffmpeg の標準入力に流し、
ffmpeg の出力からフレームごとの値を計算する。ffmpeg の起動・終了と、読み終わった後の計算は Python。
ffmpeg は映像(縮小したグレー)を標準出力に、音声(8kHz モノラル)を一時ファイルに
出す。録画が HDD にあると読み込みが計算時間の大半になる(実録画 4.3GB を
ffmpeg・解析・音声で別々に読むと各 30 秒、2026-10-08)。

フレーム番号はカット処理と同じ(映像の先頭が0、フレーム N は表示時刻が
映像開始 + (N-0.5)/fps 以上の最初のフレーム)。ソフトテレシネの録画は
デコードすると1コマが2または3フィールド分の長さになるので、ffmpeg が出す
各フレームの実際の表示時刻(-stats_mux_pre)で番号に対応付ける。

MPEG-2 は -lowres 2 (1/4 の大きさでデコード) を使う。縮小せずにデコードして
から縮小するより速い(実録画 48分 1080i で 16 秒 → 10 秒程度、2026-10-08)。

読み終わった後の計算(音量・コマとフレームの対応・場面の切り替わり・四隅の平均と標準偏差・ロゴの型と
一致度・ロゴ画像の材料)も Rust の部品(native/src/post.rs。2026-10-28 までは numpy)。値は packed.Packed、
保存は npz.py (numpy の .npz と同じ形。2026-10-30 から numpy を使わない)。
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

from ts_smartcut.autocut import npz
from ts_smartcut.autocut.logo import (
    LogoTemplate,
    recording_template_from_features,
    score_file,
    template_from_sums,
)
from ts_smartcut.autocut.logo_image import MAX_PICTURES, CornerGeometry, LogoSamples, build_samples, corner_geometry
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import Packed, as_packed
from ts_smartcut.autocut.stream_events import StreamEvents
from ts_smartcut.core.waveform import ENVELOPE_RATE, SAMPLE_RATE
from ts_smartcut.errors import FFmpegError
from ts_smartcut.system.child_processes import end_with_this_process, kill_process_tree, launch_prefix, no_window
from ts_smartcut.system.temp_file_manager import app_temp_dir, locked_run_dir, remove_stale_temp_files

logger = logging.getLogger(__name__)

# 保存形式の版。計算の方法を変えたら上げる(古い版の特徴は計算し直しの対象になる)
FEATURE_VERSION = 3  # 2: ロゴの型と一致度を追加(2026-10-08)。3: ロゴのマスクのしきい値 0.5 → 0.3(2026-10-09)

# 映像を縮小する割合(1/4)。MPEG-2 はデコーダの lowres で、それ以外は scale で縮小する
SCALE_DIVISOR = 4
LOWRES_CODECS = {"mpeg2video", "mpeg1video"}
# 四隅として切り出す大きさ(縮小後の幅・高さに対する割合)
CORNER_DIVISOR = 4
CORNER_NAMES = ("top_left", "top_right", "bottom_left", "bottom_right")

# 音量の保存形式: dB を 0.5dB 刻みの uint8 にする。値 q は q/2 - 100 dB。0 は -100dB 以下(無音)
LEVEL_FLOOR_DB = -100.0



@dataclass
class KeepRange:
    """残した区間 [start, end)。excluded は「基準外」の印(学習に使わない)。"""

    start: int
    end: int
    excluded: bool = False


@dataclass
class RecordingFeatures:
    """録画1本の特徴。scene_change はフレーム番号ごと、audio_level は 10ms ごと。"""

    fps: float
    frame_count: int  # 特徴のあるフレーム数(scene_change の長さ)
    complete: bool  # 最後まで計算できたか(途中で止めた・録画が消えた時は False)
    audio_rate: int  # audio_level の1秒あたりの点数
    audio_offset: float  # audio_level[0] の時刻(映像の先頭基準、秒)
    audio_level: Packed  # uint8、dB = q/2 - 100
    scene_change: Packed  # float16、前のフレームとの差の平均(0〜255)。0 番と、ソフトテレシネで同じコマの続きは 0
    decoded_size: Tuple[int, int]  # 縮小後の (幅, 高さ)
    corner_size: Tuple[int, int]  # 四隅1つの (幅, 高さ)
    corner_kept_mean: Packed  # float16 (4, 高さ, 幅)。CORNER_NAMES の順
    corner_kept_std: Packed
    corner_kept_count: int  # 集計したフレーム数
    corner_other_mean: Packed  # 残した区間以外(CM・番組の前後)
    corner_other_std: Packed
    corner_other_count: int
    streams: StreamEvents = field(default_factory=StreamEvents)
    # ロゴ(logo.py)。logo_score は uint8 (4, frame_count)、0〜255 が一致度 0〜1。CORNER_NAMES の順
    logo_score: Packed = field(default_factory=lambda: Packed.zeros((4, 0), "u1"))
    logo_template: Optional[LogoTemplate] = None
    # 一致度を局の型で計算したか(カットする前)。局の型は、局の型を作る材料にしない
    logo_from_station: bool = False
    version: int = FEATURE_VERSION
    timing: dict = field(default_factory=dict)  # 計算にかかった秒数と、読んだのが rust か python か(確認用)
    # 局のロゴ画像(.lgd)の材料(logo_image.py)。特徴の BLOB には入れず、DB の別の表に保存する
    logo_samples: Optional[LogoSamples] = None

    def __post_init__(self):
        self.audio_level = as_packed(self.audio_level, "u1")
        self.scene_change = as_packed(self.scene_change, "f2")
        self.corner_kept_mean = as_packed(self.corner_kept_mean, "f2")
        self.corner_kept_std = as_packed(self.corner_kept_std, "f2")
        self.corner_other_mean = as_packed(self.corner_other_mean, "f2")
        self.corner_other_std = as_packed(self.corner_other_std, "f2")
        self.logo_score = as_packed(self.logo_score, "u1")

    def audio_db(self) -> List[float]:
        """audio_level を dB にする。"""
        return [q / 2.0 + LEVEL_FLOOR_DB for q in self.audio_level.data]

    def to_blob(self) -> bytes:
        meta = {
            "version": self.version,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "complete": self.complete,
            "audio_rate": self.audio_rate,
            "audio_offset": self.audio_offset,
            "decoded_size": list(self.decoded_size),
            "corner_size": list(self.corner_size),
            "corner_kept_count": self.corner_kept_count,
            "corner_other_count": self.corner_other_count,
            "streams": self.streams.to_meta(),
            "timing": self.timing,
        }
        logo = {}
        if self.logo_template is not None:
            t = self.logo_template
            meta["logo_strength"] = [float(v) for v in t.strength]
            meta["logo_frames"] = t.frames
            meta["logo_source"] = "station" if self.logo_from_station else "recording"
            logo = {
                "logo_gx": t.gx.astype("f2"), "logo_gy": t.gy.astype("f2"),
                "logo_mask_x": t.mask_x.astype("b1"), "logo_mask_y": t.mask_y.astype("b1"),
            }
        return npz.save({
            "meta": npz.from_text(json.dumps(meta)),
            "audio_level": as_packed(self.audio_level, "u1"),
            "scene_change": as_packed(self.scene_change, "f2"),
            "corner_kept_mean": as_packed(self.corner_kept_mean, "f2"),
            "corner_kept_std": as_packed(self.corner_kept_std, "f2"),
            "corner_other_mean": as_packed(self.corner_other_mean, "f2"),
            "corner_other_std": as_packed(self.corner_other_std, "f2"),
            "caption_times": Packed.from_values(self.streams.caption_times, "f4"),
            "logo_score": as_packed(self.logo_score, "u1"),
            **logo,
        })

    @classmethod
    def from_blob(cls, blob: bytes) -> "RecordingFeatures":
        data = npz.load(blob)
        meta = json.loads(npz.text(data["meta"]))
        template = recording_template_from_features(blob, include_station=True)
        return cls(
            fps=meta["fps"],
            frame_count=meta["frame_count"],
            complete=meta["complete"],
            audio_rate=meta["audio_rate"],
            audio_offset=meta["audio_offset"],
            audio_level=data["audio_level"],
            scene_change=data["scene_change"],
            decoded_size=tuple(meta["decoded_size"]),
            corner_size=tuple(meta["corner_size"]),
            corner_kept_mean=data["corner_kept_mean"],
            corner_kept_std=data["corner_kept_std"],
            corner_kept_count=meta["corner_kept_count"],
            corner_other_mean=data["corner_other_mean"],
            corner_other_std=data["corner_other_std"],
            corner_other_count=meta["corner_other_count"],
            streams=StreamEvents.from_meta(meta.get("streams", {}), data["caption_times"].astype("f4").flat()),
            logo_score=data["logo_score"] if "logo_score" in data else Packed.zeros((4, 0), "u1"),
            logo_template=template,
            logo_from_station=meta.get("logo_source") == "station",
            version=meta["version"],
            timing=meta.get("timing", {}),
        )


def _bytes_of(values, dtype: str) -> bytes:
    """Packed・バイト列のほか、テストの numpy の配列 (型をそろえたもの) も受け取る。"""
    if isinstance(values, Packed):
        return values.astype(dtype).data
    if isinstance(values, (bytes, bytearray)):
        return bytes(values)
    return memoryview(values).cast("B").tobytes()


def peaks_to_level(peaks) -> Packed:
    """10ms ごとのピーク(int16 の目盛り)を、0.5dB 刻みの uint8 にする(native/src/post.rs)。"""
    return Packed(native_module().peaks_to_levels(_bytes_of(peaks, "i2")), "u1")


@dataclass
class _Probe:
    codec: str
    width: int
    height: int
    video_start: float
    format_start: float
    has_audio: bool


def _probe(file_path: Path, ffprobe_path: Union[str, Path]) -> _Probe:
    result = subprocess.run(
        [str(ffprobe_path), "-v", "quiet",
         "-show_entries", "format=start_time:stream=codec_type,codec_name,width,height,start_time",
         "-of", "json", str(file_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **no_window(),
    )
    data = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError("E101", key="exceptions.features_no_video", path=file_path)

    def _float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    video_start = _float(video.get("start_time"), 0.0)
    return _Probe(
        codec=str(video.get("codec_name", "")),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        video_start=video_start,
        format_start=_float(data.get("format", {}).get("start_time"), video_start),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def increasing_pictures(pts) -> Packed:
    """表示時刻がそれまでのどのコマより後のコマの番号。時刻が逆戻り(同じ時刻も)したコマを除く。"""
    return Packed(native_module().increasing_pictures(_bytes_of(pts, "f8")), "i8")


def map_pictures_to_frames(pts, video_start: float, fps: float) -> Tuple[Packed, Packed]:
    """デコードしたコマの表示時刻(秒、昇順)から、フレーム番号ごとのコマの番号と、
    そのフレームがコマの最初のフレームか、を返す。

    フレーム N は「表示時刻 >= 映像開始 + (N-0.5)/fps」の最初のコマ(カット処理の
    _boundary() と同じ)。最後のコマより後のフレームは含めない。"""
    idx, first = native_module().map_pictures_to_frames(_bytes_of(pts, "f8"),
                                                        float(video_start), float(fps))
    return Packed(idx, "i8"), Packed(first, "b1")


class _LogoImageReader:
    """残した区間のキーフレームを元の解像度・カラーの四隅(モザイク)で読む 2 つ目の ffmpeg
    (logo_image.py の材料)。フレームは偏りなく最大 MAX_PICTURES 枚を一時ファイルに残す
    (リザーバー抽出。乱数の種は固定なので、同じ録画なら同じフレームを選ぶ)。

    出力を読んで一時ファイルに残すのは Rust(ts_smartcut_native)。このクラスは ffmpeg を起動・終了し、
    Rust が使う乱数(choose_slot)を持つ。"""

    def __init__(self, ffmpeg_path: Union[str, Path], geometry: CornerGeometry, temp_dir: Path,
                 video_start: float, fps: float, keep: Sequence[KeepRange], stop_event: threading.Event):
        self.geometry = geometry
        self.spool_path = temp_dir / "logo_image.yuv"
        self.video_start = video_start
        self.fps = fps
        self.keep = keep
        self.stop_event = stop_event
        self.stats_path = temp_dir / "keyframes.txt"
        self.count = 0  # 一時ファイルのフレーム数
        self.pictures = 0  # 残した区間のキーフレームの数
        self.error: Optional[str] = None
        # 置き換える位置を選ぶ乱数(MAX_PICTURES 枚より後のキーフレームで使う)
        # (2026-10-30 までは numpy の乱数。選ぶキーフレームが変わるだけで、偏りなく選ぶのは同じ)
        self.rng = random.Random(0)
        cmd = [
            *launch_prefix(), str(ffmpeg_path), "-v", "error", "-copyts", "-skip_frame", "nokey",
            "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", geometry.filter(),
            # 四隅を並べるフィルター(split・stack)を通ると、入力の時刻({ptsi})と demux の時間の単位が
            # 取れなくなる(NOPTS)。出力の時刻({pts})は 1/90000 で正しく出る
            "-fps_mode", "passthrough", "-enc_time_base", "1/90000",
            "-f", "rawvideo", "-stats_mux_pre", str(self.stats_path), "-stats_mux_pre_fmt", "{pts} {tb}", "pipe:1",
        ]
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        **no_window())
        end_with_this_process(self.process)
        self._stderr: List[bytes] = []
        self._stderr_thread = threading.Thread(target=lambda: self._stderr.append(self.process.stderr.read()),
                                               daemon=True)
        self._stderr_thread.start()

    def choose_slot(self, n: int) -> int:
        """リザーバー抽出で置き換える位置(0〜n-1。MAX_PICTURES 以上なら残さない)。"""
        return self.rng.randrange(n)

    def kill(self) -> None:
        if self.process.poll() is None:
            kill_process_tree(self.process)

    def wait(self) -> None:
        _wait_or_kill(self.process)

    def finish(self) -> bool:
        """ffmpeg の終わりを待つ。最後まで読めたら True。"""
        self.wait()
        self._stderr_thread.join(timeout=5)
        if self.process.returncode != 0:
            message = b"".join(self._stderr).decode("utf-8", errors="replace").strip()
            logger.warning("The logo image reader failed (%s): %s", self.process.returncode, message[-500:])
            return False
        return self.error is None


@dataclass
class _Shape:
    """縮小した映像 (w, h) と四隅 1 つ (cw, ch) の大きさ。"""

    w: int
    h: int
    cw: int
    ch: int

    @property
    def logo_shape(self) -> Tuple[int, int]:
        return self.ch // 2, self.cw // 2


@dataclass
class _Reading:
    """録画を読んでいる間に集めた値(Rust が返したもの)。"""

    diffs: bytes  # float32、コマごとの場面の切り替わり
    pts: bytes  # float64、コマごとの表示時刻
    kept_sum: bytes  # float64 (4, ch, cw)
    kept_sq: bytes
    other_sum: bytes
    other_sq: bytes
    kept_n: int
    other_n: int
    template_sums: Tuple[bytes, bytes, int]  # 残した区間の縁の向きの合計 (sum_x, sum_y は int64)、フレーム数
    stopped: bool  # 止めた
    feed_complete: bool  # 録画の最後まで ffmpeg に渡せた
    feed_error: Optional[str]
    streams: StreamEvents


def _wait_or_kill(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        kill_process_tree(process)
        process.wait()


def _hand_over(stream) -> int:
    """パイプを ts_smartcut_native に渡す。OS のハンドル(Linux はファイル記述子)を複製して渡し、
    Python 側は閉じる(標準入力は、両方が閉じるまで ffmpeg に入力の終わりが伝わらない)。"""
    fd = stream.fileno()
    if sys.platform == "win32":
        import _winapi
        import msvcrt

        current = _winapi.GetCurrentProcess()
        handle = _winapi.DuplicateHandle(current, msvcrt.get_osfhandle(fd), current, 0, False,
                                         _winapi.DUPLICATE_SAME_ACCESS)
    else:
        handle = os.dup(fd)
    stream.close()
    return handle


def _read_native(native, file_path: Path, process: subprocess.Popen, image_reader: Optional[_LogoImageReader],
                 shape: _Shape, stats_path: Path, logo_path: Path, video_start: float, fps: float,
                 keep: Sequence[KeepRange], service_id: Optional[int], stop_event: threading.Event,
                 progress: Optional[Callable[[int], None]]) -> _Reading:
    """ffmpeg のフレームを Rust (ts_smartcut_native) で読んで計算する。録画を読んで ffmpeg に渡すのも、
    TS の解析も Rust。ffmpeg を止める・終わりを待つのは Python。

    2026-10-28 まで同じ処理を Python でも持っていたが、TS の解析・フレームの計算と、パイプの読み書きが
    GIL を取り合い、計算の間は ffmpeg も出力を待つので、ffmpeg だけの時間の 3 倍近くかかっていた。"""
    image = {}
    if image_reader is not None:
        image = dict(
            image_in=_hand_over(image_reader.process.stdin), image_out=_hand_over(image_reader.process.stdout),
            image_stats_path=str(image_reader.stats_path), mosaic_bytes=image_reader.geometry.mosaic_bytes,
            spool_path=str(image_reader.spool_path), max_pictures=MAX_PICTURES,
            image_alive=lambda: image_reader.process.poll() is None and not stop_event.is_set(),
            choose_slot=image_reader.choose_slot,
        )
    reader = native.FeatureReader(
        str(file_path), _hand_over(process.stdin), _hand_over(process.stdout), str(stats_path),
        shape.w, shape.h, shape.cw, shape.ch, video_start, fps, [(r.start, r.end, r.excluded) for r in keep],
        service_id, str(logo_path), **image)

    def tick(pictures: int) -> bool:
        if pictures and progress is not None:
            progress(pictures)
        return stop_event.is_set()

    def kill() -> None:
        if process.poll() is None:
            kill_process_tree(process)
        if image_reader is not None:
            image_reader.kill()

    # 止めたら、次のフレームが届くのを待たずに ffmpeg を終わらせる(read_frames が止めるかを尋ねるのは
    # フレームを読んだ時なので、PC が重くて ffmpeg の出力が途切れていると、止めるのが 10 秒以上遅れた)
    reading_done = threading.Event()

    def watch() -> None:
        # stop_event は is_set だけを使う(FeatureWorker は is_set だけのものを渡す)
        while not reading_done.wait(0.1):
            if stop_event.is_set():
                kill()
                return

    watcher = threading.Thread(target=watch, name="autocut-stop-watcher", daemon=True)
    watcher.start()
    stopped = True  # read_frames が例外を出した時も ffmpeg を止める
    try:
        stopped = reader.read_frames(tick, lambda: process.poll() is None and not stop_event.is_set())
        stopped = stopped or stop_event.is_set()
    finally:
        reading_done.set()
        watcher.join()
        if stopped:
            kill()
        _wait_or_kill(process)
        if image_reader is not None:
            image_reader.wait()
        result = reader.finish()

    if result["read_error"]:
        logger.warning("The recording became unreadable while computing features: %s (%s)", file_path,
                       result["read_error"])
    if result["scan_error"]:
        logger.warning("Could not parse audio/caption changes: %s (%s)", file_path, result["scan_error"])
    if image_reader is not None:
        image_reader.count = result["image_count"]
        image_reader.pictures = result["image_pictures"]
        image_reader.error = result["image_error"]
        if image_reader.error:
            logger.warning("Could not collect logo image samples: %s", image_reader.error)
        if not result["image_input_ok"]:
            logger.warning("The logo image reader stopped taking input: %s", file_path)

    template_sums = (result["template_sum_x"], result["template_sum_y"], result["template_count"])
    feed_complete = result["feed_complete"]
    streams = StreamEvents.from_native(result["pmt_changes"], result["audio_channels"],
                                       Packed(result["captions"], "f8").flat(),
                                       complete=feed_complete and not result["scan_error"])
    return _Reading(
        diffs=bytes(result["diffs"]), pts=bytes(result["pts"]),
        kept_sum=bytes(result["kept_sum"]), kept_sq=bytes(result["kept_sq"]),
        other_sum=bytes(result["other_sum"]), other_sq=bytes(result["other_sq"]),
        kept_n=result["kept_n"], other_n=result["other_n"], template_sums=template_sums,
        stopped=stopped, feed_complete=feed_complete, feed_error=result["feed_error"], streams=streams,
    )


def compute_features(
    file_path: Path,
    fps: float,
    keep: Sequence[KeepRange],
    ffmpeg_path: Union[str, Path] = "ffmpeg",
    ffprobe_path: Union[str, Path] = "ffprobe",
    stop_event: Optional[threading.Event] = None,
    progress: Optional[Callable[[int], None]] = None,
    service_id: Optional[int] = None,
    temp_base: Optional[Path] = None,
    station_template: Optional[LogoTemplate] = None,
) -> RecordingFeatures:
    """録画1本の特徴を計算する。stop_event が立つか録画が読めなくなったら、
    そこまでの分で complete=False の結果を返す。service_id は録画した番組の
    サービス(PMT を見る番組。None なら PAT の最初の番組)。

    ロゴの一致度は、残した区間(keep)があればその録画自身の型で、無ければ(カットする前。
    A6 の提案) station_template(局の型)で計算する。

    一時ファイル(フレームの時刻、音声。50分で約 50MB)は temp_base(既定はアプリの
    一時フォルダ)の run_<id>/ に作る。異常終了で残った run_ フォルダは次の計算の
    始めに消す(バッチ処理の一時フォルダと同じロックの仕組み)。"""
    base = Path(temp_base) if temp_base is not None else app_temp_dir()
    remove_stale_temp_files(base)
    with locked_run_dir(base) as temp_dir:
        return _compute_features(
            Path(file_path), fps, keep, ffmpeg_path, ffprobe_path,
            stop_event or threading.Event(), progress, service_id, temp_dir, station_template,
        )


def _compute_features(
    file_path: Path,
    fps: float,
    keep: Sequence[KeepRange],
    ffmpeg_path: Union[str, Path],
    ffprobe_path: Union[str, Path],
    stop_event: threading.Event,
    progress: Optional[Callable[[int], None]],
    service_id: Optional[int],
    temp_dir: Path,
    station_template: Optional[LogoTemplate] = None,
) -> RecordingFeatures:
    started = time.monotonic()
    native = native_module()  # 無ければ E506(ffmpeg を起動する前に)

    probe = _probe(file_path, ffprobe_path)
    w = -(-probe.width // SCALE_DIVISOR)
    h = -(-probe.height // SCALE_DIVISOR)
    cw, ch = w // CORNER_DIVISOR, h // CORNER_DIVISOR

    stats_path = temp_dir / "frames.txt"
    audio_path = temp_dir / "audio.s16"
    logo_path = temp_dir / "corners.u8"  # ロゴの一致度用の 1/8 の四隅(50分で約 540MB)
    logo_shape = (ch // 2, cw // 2)
    pre = ["-lowres", "2"] if probe.codec in LOWRES_CODECS and SCALE_DIVISOR == 4 else []
    cmd = [
        *launch_prefix(), str(ffmpeg_path), "-v", "error", "-copyts", *pre, "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"scale={w}:{h}:flags=area,format=gray",
        "-fps_mode", "passthrough", "-enc_time_base", "demux",
        "-f", "rawvideo", "-stats_mux_pre", str(stats_path), "-stats_mux_pre_fmt", "{ptsi} {tb}", "pipe:1",
    ]
    if probe.has_audio:
        # ファイル全体の先頭(多くは音声)を 0 にずらし、そこから無音で埋めて始め、途中の
        # 途切れも埋めるので、サンプルの位置がそのまま時刻になる(waveform.py と同じ)。
        # -copyts のままの時刻で first_pts を指定しても効かず、時刻 0 から埋めた
        # (放送の時刻は数時間あるので、数百 MB の無音になる。2026-10-08)。
        # 始まりより前になった音声は捨てる: 標準入力から読むと、始めに壊れた音声がある録画では
        # 最初の 1 コマの時刻が 0 になり(ファイルを直接開くと起きない)、ずらすと数万秒前に
        # なって、aresample が残り全部を捨て、音量が空になった(GR-D・BS-B、2026-10-11)
        cmd += [
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1",
            "-af", f"asetpts=PTS-{probe.format_start!r}/TB,aselect=gte(t\\,0),"
                   f"aresample={SAMPLE_RATE}:async=1:first_pts=0",
            "-f", "s16le", str(audio_path),
        ]

    # 局のロゴ画像の材料: 残した区間があり、四隅の位置が型と合う大きさの映像だけ
    image_reader: Optional[_LogoImageReader] = None
    geometry = corner_geometry(probe.width, probe.height, (cw, ch), SCALE_DIVISOR)
    if any(not r.excluded for r in keep):
        if geometry is None:
            logger.info("Not collecting logo image samples (frame size %dx%d): %s",
                        probe.width, probe.height, file_path)
        else:
            try:
                image_reader = _LogoImageReader(ffmpeg_path, geometry, temp_dir, probe.video_start, fps, keep,
                                                stop_event)
            except OSError as e:
                logger.warning("Could not start the logo image reader: %s (%s)", file_path, e)

    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               **no_window())
    end_with_this_process(process)
    stderr_chunks: List[bytes] = []
    stderr_thread = threading.Thread(target=lambda: stderr_chunks.append(process.stderr.read()), daemon=True)
    stderr_thread.start()
    shape = _Shape(w, h, cw, ch)
    reading = _read_native(native, file_path, process, image_reader, shape, stats_path, logo_path, probe.video_start, fps,
                   keep, service_id, stop_event, progress)
    stderr_thread.join(timeout=5)
    image_ok = image_reader.finish() if image_reader is not None else False
    stopped = reading.stopped

    returncode = process.returncode
    if returncode != 0 and not stopped:
        message = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        logger.warning("ffmpeg returned an error while computing features (%s): %s %s", returncode, file_path, message[-500:])
    if reading.feed_error and not stopped:
        logger.warning("The recording became unreadable while computing features: %s (%s)", file_path,
                       reading.feed_error)
    complete = not stopped and returncode == 0 and reading.feed_complete
    kept_n, other_n = reading.kept_n, reading.other_n

    # 音量(一時ファイル。途中で止めた時もそこまでの分を使う。一時フォルダごと呼び出し元が消す)
    audio_level = Packed.zeros((0,), "u1")
    if audio_path.exists():
        audio_level = Packed(native.audio_levels_from_file(str(audio_path)), "u1")
    if probe.has_audio and len(audio_level) == 0:
        logger.warning("Could not compute the audio level: %s", file_path)

    # コマごとの値を、カット処理と同じフレーム番号ごとの値にする。時刻が逆戻りしたコマ(受信エラーの _drop
    # 録画で壊れたコマ等)は飛ばして続ける(2026-10-12 までは逆戻りした所で打ち切っていて、CS-B等の特徴が
    # 途中で止まっていた)
    picture_count = min(len(reading.pts) // 8, len(reading.diffs) // 4)
    idx_raw, scene_raw, skipped, first_skipped = native.picture_frames(
        reading.pts, reading.diffs, float(probe.video_start), float(fps))
    if skipped:
        logger.warning("Skipped %d pictures whose timestamps went backwards (first at picture %d): %s",
                       skipped, first_skipped, file_path)
    idx = bytes(idx_raw)  # 元のコマの番号 (int64。ロゴの一致度・場面の変化はコマの順に並んでいる)
    scene = Packed(scene_raw, "f2")

    # ロゴの一致度: 録画自身の残した区間から作った型で、コマごとに計算してフレーム番号ごとにする
    logo_started = time.monotonic()
    sum_x, sum_y, template_count = reading.template_sums
    template = template_from_sums(sum_x, sum_y, logo_shape, template_count)
    from_station = False
    if template_count == 0 and station_template is not None:
        if (station_template.gx.shape, station_template.gy.shape) == (template.gx.shape, template.gy.shape):
            template, from_station = station_template, True
        else:
            logger.info("Not scoring the logo because the station template has a different frame size: %s", file_path)
    picture_scores = score_file(logo_path, picture_count, logo_shape, template)
    logo_score = Packed(native.frame_scores(picture_scores, picture_count, idx), "u1", (4, len(idx) // 8))
    logo_seconds = time.monotonic() - logo_started

    # 局のロゴ画像の材料: 録画自身の型(残した区間から作った型)のロゴの周りだけを合計する
    image_started = time.monotonic()
    logo_samples = None
    if image_reader is not None and image_ok and not stopped and not from_station:
        try:
            logo_samples = build_samples(image_reader.spool_path, image_reader.count, image_reader.pictures,
                                         image_reader.geometry, template)
            if logo_samples is None:
                logger.info("No logo image samples (%d keyframes read, logo corners %s): %s", image_reader.count,
                            template.has_logo, file_path)
        except Exception:  # noqa: BLE001 - ロゴ画像の材料の失敗で特徴の計算は止めない
            logger.warning("Could not build logo image samples: %s", file_path, exc_info=True)
    image_seconds = time.monotonic() - image_started

    corner_shape = (4, ch, cw)

    def _mean_std(total, square, n):
        mean, std = native.corner_mean_std(total, square, int(n))
        return Packed(mean, "f2", corner_shape), Packed(std, "f2", corner_shape)

    km, ks = _mean_std(reading.kept_sum, reading.kept_sq, kept_n)
    om, os_ = _mean_std(reading.other_sum, reading.other_sq, other_n)
    streams = reading.streams
    return RecordingFeatures(
        fps=fps,
        frame_count=len(scene),
        complete=complete,
        audio_rate=ENVELOPE_RATE,
        audio_offset=probe.format_start - probe.video_start,
        audio_level=audio_level,
        scene_change=scene,
        decoded_size=(w, h),
        corner_size=(cw, ch),
        corner_kept_mean=km,
        corner_kept_std=ks,
        corner_kept_count=kept_n,
        corner_other_mean=om,
        corner_other_std=os_,
        corner_other_count=other_n,
        streams=streams,
        logo_score=logo_score,
        logo_template=template,
        logo_from_station=from_station,
        timing={"total": round(time.monotonic() - started, 2), "logo": round(logo_seconds, 2),
                "logo_image": round(image_seconds, 2)},
        logo_samples=logo_samples,
    )
