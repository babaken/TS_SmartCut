"""ffmpeg/ffprobe wrapper (design doc §3), plus VFR detection (§16.1) and
keyframe caching (§6.2, §26.1).

Keyframe lookups are cached in ``KeyframeCache/`` next to the executable
(not next to the recording; user decision 2026-10-09), keyed by the source's
name, size and mtime, to avoid re-shelling out to ``ffprobe -skip_frame nokey``
on every open. Caches unused for 30 days are deleted at startup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from ts_smartcut.errors import FFprobeError, MuxFailedError, ProcessingCancelledError
from ts_smartcut.ffmpeg.compat import NO_FRAMES, progress_frames
from ts_smartcut.models import AppSettings, FileCutInfo
from ts_smartcut.system.child_processes import (
    StalledProcessError,
    end_with_this_process,
    kill_process_tree,
    launch_prefix,
    run_until_stalled,
    no_window,
)
from ts_smartcut.system.temp_file_manager import keyframe_cache_dir
from ts_smartcut.time_utils import time_to_frame

logger = logging.getLogger(__name__)

# ファイル全体を読む ffprobe/ffmpeg が、この秒数の間なにも出力しなければ止める(run_until_stalled)
STALL_SECONDS = 60.0

KEYFRAME_CACHE_SUFFIX = ".keyframes.json"
# 最後に使ってから、この日数たったキャッシュを起動時に消す(利用者の決定 2026-10-09)
KEYFRAME_CACHE_RETENTION_DAYS = 30
# キャッシュのファイル名に残す録画名の長さ(文字)。Linux はファイル名が 255 バイトまでで、
# 日本語は 1 文字 3 バイトなので、長い番組名をそのまま使うと超える
_KEYFRAME_CACHE_NAME_CHARS = 40

# 失敗時のメッセージに残すffmpegの出力の行数(末尾)
_STDERR_TAIL_LINES = 200

# VFR判定の許容誤差。avg_frame_rateとr_frame_rateの差がこれを超えたらVFRとみなす。
_VFR_TOLERANCE = 0.01

# ffprobeのfield_orderのうちインターレースを表す値
# (tt/bb: トップ/ボトムフィールドが先、tb/bt: 符号化順と表示順が逆)
INTERLACED_FIELD_ORDERS = frozenset({"tt", "bb", "tb", "bt"})


# get_scan_type()の戻り値
SCAN_PROGRESSIVE = "progressive"
SCAN_INTERLACED = "interlaced"
SCAN_SOFT_TELECINE = "soft_telecine"  # プログレッシブ+フィールド繰り返し指示(3:2)
SCAN_MIXED = "mixed"  # インターレースとソフトテレシネが混在(CS-A等)


@dataclass(frozen=True)
class SourceTiming:
    """カット処理に必要なソースの時刻・走査情報。"""

    video_start: float  # 映像ストリームのstart_time(秒)
    format_start: float  # ファイル全体のstart_time(秒) — ffmpegの-ssはこれが基準
    field_order: str  # ffprobeのfield_order (tt/bb/tb/bt/progressive/unknown)
    has_audio: bool
    audio_packet_duration: float  # 先頭音声ストリームの1パケットの長さ(秒)。音声なしなら0
    has_b_frames: bool = False  # 映像が表示順と異なる順で格納されるフレーム(Bフレーム)を持つか
    # 先頭以外も含めて調べた結果、フィールド単位で表示される映像か
    # (インターレースのフレーム、またはソフトテレシネのフィールド繰り返し
    # 指示を持つフレームがある)。field_orderは先頭付近しか見ないため、
    # ソフトテレシネ区間から始まる録画では"progressive"になってしまう。
    field_based: bool = False
    top_field_first: bool = True

    @property
    def seek_offset(self) -> float:
        """フレーム番号(映像start_time基準)の時刻を、ffmpegの-ss(ファイル
        start_time基準)へ変換するときに足す値。TSでは音声が映像より先に
        始まることが多く、この差を無視すると全シークがずれる。"""
        return max(0.0, self.video_start - self.format_start)

    @property
    def interlaced(self) -> bool:
        return self.field_based or self.field_order in INTERLACED_FIELD_ORDERS


@dataclass(frozen=True)
class VideoFrameInfo:
    """デコードしたフレーム1枚の表示時刻と走査情報。"""

    pts: float  # 絶対時刻(秒)
    progressive: bool
    top_field_first: bool
    repeat_first_field: bool


def _parse_frame_rate(rate_str: str) -> float:
    """"25/1" や "30000/1001" 形式の文字列をfloatへ変換する。"""
    if "/" in rate_str:
        num_str, _, den_str = rate_str.partition("/")
        num, den = float(num_str), float(den_str)
        return num / den if den else 0.0
    return float(rate_str)


def _is_vfr_from_rates(avg_frame_rate: str, r_frame_rate: str) -> bool:
    """avg_frame_rate と r_frame_rate を比較してVFRかどうかを判定する
    純粋関数（design doc §16.1）。実ハードウェア・ファイル不要でテスト可能。"""
    r = _parse_frame_rate(r_frame_rate)
    avg = _parse_frame_rate(avg_frame_rate)
    if r == 0:
        return False
    if _is_field_rate(avg, r):
        return False
    return abs(avg - r) > _VFR_TOLERANCE


def _is_field_rate(avg: float, r: float) -> bool:
    """r_frame_rateがフレームレートの2倍(=フィールドレート)で報告されているか。
    ソフトテレシネ(プログレッシブ+フィールド繰り返し指示)のMPEG-2では、
    ffprobeはr_frame_rateを59.94、avg_frame_rateを29.97と報告する。これは
    フィールド単位で一定のレートで、VFRではない(CS-Aの実録画と同じ構造の
    素材で確認、2026-10-04)。"""
    return avg > 0 and abs(r - 2 * avg) <= _VFR_TOLERANCE * 2


def _video_frame_rate(video_stream: dict) -> float:
    """フレーム番号の基準にするフレームレート。r_frame_rateがフィールド
    レートで報告されている場合はavg_frame_rate(=フレームレート)を使う。"""
    r = _parse_frame_rate(video_stream.get("r_frame_rate", "0/1"))
    avg = _parse_frame_rate(video_stream.get("avg_frame_rate", "0/1"))
    return avg if _is_field_rate(avg, r) else r


def merge_field_pictures(
    packets: List[Tuple[float, float, bool]], fps: float
) -> List[Tuple[float, float, bool]]:
    """デコード順の映像パケット [(表示時刻, 長さ, キーフレームか)] の、フィールドごとの
    パケットの組を 1 フレーム(最初のフィールドの時刻, 2 つの長さの和, 最初のキーフレームの印)
    にまとめる。

    H.264 のインターレースの録画には、1 フレームを 2 つのフィールドの絵として別々のパケットに
    書くものがある(テスト動画 2 本、2026-11-17)。2 つ目のフィールドの時刻はちょうど次の
    フレームの境目(半フレーム手前)に当たり、区間の境目がそこに来て、コピーした部分の
    先頭と最後に片方のフィールドだけが入っていた(出力が約 150 枚欠けた)。フィールドの
    パケットは長さが半フレームで、続くパケットが長さの分だけ後に表示されるもの。"""
    field_limit = 0.75 / fps
    merged: List[Tuple[float, float, bool]] = []
    index = 0
    while index < len(packets):
        pts, duration, key = packets[index]
        if index + 1 < len(packets) and 0 < duration < field_limit:
            next_pts, next_duration, _ = packets[index + 1]
            if 0 < next_duration < field_limit and abs(next_pts - (pts + duration)) < 0.25 / fps:
                merged.append((pts, duration + next_duration, key))
                index += 2
                continue
        merged.append((pts, duration, key))
        index += 1
    return merged


def _keyframe_cache_path(file_path: Path, stat: os.stat_result) -> Path:
    """録画のキャッシュのパス。名前・大きさ・更新日時から作るので、録画を別のフォルダへ
    移してもそのまま使え、録画が変われば別のファイルになる。"""
    key = f"{file_path.name}\0{stat.st_size}\0{stat.st_mtime_ns}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    stem = file_path.name[:_KEYFRAME_CACHE_NAME_CHARS]
    return keyframe_cache_dir() / f"{stem}_{digest}{KEYFRAME_CACHE_SUFFIX}"


def _load_keyframe_cache(file_path: Path) -> Optional[List[int]]:
    stat = file_path.stat()
    cache_path = _keyframe_cache_path(file_path, stat)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        data.get("source_name") != file_path.name
        or data.get("source_size") != stat.st_size
        or data.get("source_mtime_ns") != stat.st_mtime_ns
    ):
        return None
    # 最後に使った日をファイルの日付で残す(30 日使わなければ消す)
    try:
        os.utime(cache_path)
    except OSError:
        pass
    return data.get("keyframes")


def _save_keyframe_cache(file_path: Path, keyframes: List[int]) -> None:
    """書けなくても録画は開ける(次も ffprobe で調べ直すだけ)ので、警告だけ残す。
    一時ファイルに書いてから置き換え、2 つ目のアプリが書きかけを読まないようにする。"""
    stat = file_path.stat()
    data = {
        "source_name": file_path.name,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "keyframes": keyframes,
    }
    try:
        cache_path = _keyframe_cache_path(file_path, stat)
        tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_path, cache_path)
    except OSError as e:
        logger.warning("Could not save the keyframe cache: %s (%s)", file_path.name, e)


def remove_old_keyframe_caches(
    retention_days: int = KEYFRAME_CACHE_RETENTION_DAYS, now: Optional[float] = None
) -> int:
    """最後に使ってから retention_days 日たったキャッシュ(と書きかけの一時ファイル)を消し、
    消した数を返す。起動時に裏のスレッドで呼ぶ。録画が見つからないことでは消さない
    (共有の PC が止まっている時がある)。"""
    limit = (time.time() if now is None else now) - retention_days * 86400
    removed = 0
    try:
        entries = list(keyframe_cache_dir().iterdir())
    except OSError as e:
        logger.warning("Could not read the keyframe cache folder (%s)", e)
        return 0
    for path in entries:
        name = path.name
        if not (name.endswith(KEYFRAME_CACHE_SUFFIX) or (KEYFRAME_CACHE_SUFFIX + "." in name and name.endswith(".tmp"))):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Deleted %d unused keyframe caches", removed)
    return removed


class FFmpegWrapper:
    def __init__(self, settings: Optional[AppSettings] = None):
        self.settings = settings
        # setすると実行中のffmpegを終了させ、run()はProcessingCancelledError(E504)
        # を送出する(以後のrun()も同じ)。中止ボタン用。使う側がclear()する。
        self.cancel_event = threading.Event()
        self.ffmpeg_path: Union[str, Path] = (
            settings.ffmpeg_path if settings and settings.ffmpeg_path else "ffmpeg"
        )
        self.ffprobe_path: Union[str, Path] = (
            settings.ffprobe_path if settings and settings.ffprobe_path else "ffprobe"
        )

    # ---- ffmpeg 実行 -----------------------------------------------

    def run(
        self,
        args: List[str],
        progress_callback: Optional[Callable[[int], None]] = None,
        bytes_per_frame: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        """ffmpegを実行する。progress_callbackは出力済みの映像フレーム数を受け取る
        (約0.5秒ごと。音声だけの出力では呼ばれない)。bytes_per_frame を渡すと、
        frame= を出さない ffmpeg(6.x のコピー)でも、書いた大きさから枚数を見積もって渡す。"""
        cmd = [str(self.ffmpeg_path), *args]
        if self.cancel_event.is_set():
            raise ProcessingCancelledError("E504", key="exceptions.cancelled")

        # 放送TSでは番組情報(ARIB文字コード)等がそのままメッセージに出力され、
        # UTF-8として読めないバイトを含む。厳密に読むと処理全体が落ちる
        # (実録画で確認、2026-10-04)ため、読めない文字は置き換える。
        # Windowsでは既定がcp932になるのでencodingも明示する。
        process = subprocess.Popen(
            [*launch_prefix(), *cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **no_window(),
        )
        # アプリが強制終了されたらffmpegも終わらせる(Windowsはジョブオブジェクト、
        # Linuxはlaunch_prefixのsetpriv。残ったffmpegが一時ファイルを書き続けていた)
        end_with_this_process(process)
        # 中止されたら終了させる。pdeathsig(Linux)は起動したこのスレッドの終了で
        # 送られるので、終了の待ち合わせはこのスレッドのまま行う。
        threading.Thread(target=self._kill_on_cancel, args=(process,), daemon=True).start()

        head = ""
        if progress_callback:
            head = self._monitor_progress(process, progress_callback, bytes_per_frame)

        stdout, stderr = process.communicate()
        # 進捗を読んだ場合はその分のメッセージも失敗時の説明に含める(以前は
        # 読み捨てられ、エラーが「ffmpeg実行エラー」だけになっていた)
        stderr = head + stderr

        if self.cancel_event.is_set():
            raise ProcessingCancelledError("E504", key="exceptions.cancelled")
        if process.returncode != 0:
            raise MuxFailedError("E101", stderr)

        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

    def _kill_on_cancel(self, process: subprocess.Popen) -> None:
        while process.poll() is None:
            if self.cancel_event.wait(0.2):
                kill_process_tree(process)
                return

    def _monitor_progress(
        self,
        process: subprocess.Popen,
        callback: Callable[[int], None],
        bytes_per_frame: Optional[float] = None,
    ) -> str:
        """ffmpegの標準エラー出力から出力済みフレーム数を読み、callbackへ渡す。
        読んだ出力の末尾(失敗時の説明用)を返す。進捗の行は
で区切られるが、
        text=Trueの読み込みが改行として扱う。"""
        tail: List[str] = []
        for line in iter(process.stderr.readline, ""):
            # 進捗の行の読み方(版による違い)は compat.py
            frames = progress_frames(line, bytes_per_frame)
            if frames is not None:
                if frames != NO_FRAMES:
                    callback(frames)
                continue
            tail.append(line)
            if len(tail) > _STDERR_TAIL_LINES:
                del tail[0]
        return "".join(tail)

    # ---- ffprobe 情報取得 --------------------------------------------

    def probe(self, file_path: Path, probe_args: List[str] = ()) -> dict:
        """ffprobeの生JSON出力(format+streams)を返す。probe_args で読み取りの幅を変えられる
        (番組の途中の PMT で加わる音声を見つける時)。"""
        cmd = [
            str(self.ffprobe_path),
            "-v", "quiet",
            *probe_args,
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **no_window())
        if result.returncode != 0:
            raise FFprobeError("E102", result.stderr)
        return json.loads(result.stdout)

    def get_file_info(self, file_path: Path) -> FileCutInfo:
        """ファイル情報を取得しFileCutInfoを構築する。"""
        file_path = Path(file_path)
        data = self.probe(file_path)
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video_stream is None:
            raise FFprobeError("E102", key="exceptions.no_video_stream", path=file_path)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        fps = _video_frame_rate(video_stream)
        duration = float(fmt.get("duration") or video_stream.get("duration") or 0.0)

        nb_frames_raw = video_stream.get("nb_frames")
        total_frames = int(nb_frames_raw) if nb_frames_raw is not None else round(duration * fps)

        bitrate_raw = fmt.get("bit_rate") or video_stream.get("bit_rate")
        bitrate = int(bitrate_raw) if bitrate_raw is not None else 0

        file_size_raw = fmt.get("size")
        file_size = int(file_size_raw) if file_size_raw is not None else file_path.stat().st_size

        return FileCutInfo(
            file_path=file_path,
            total_frames=total_frames,
            fps=fps,
            resolution=(int(video_stream.get("width", 0)), int(video_stream.get("height", 0))),
            video_codec=video_stream.get("codec_name", ""),
            audio_codec=audio_stream.get("codec_name", "") if audio_stream else "",
            bitrate=bitrate,
            duration=duration,
            file_size=file_size,
            is_vfr=_is_vfr_from_rates(
                video_stream.get("avg_frame_rate", "0/1"), video_stream.get("r_frame_rate", "0/1")
            ),
        )

    def get_video_start_offset(self, file_path: Path) -> float:
        """映像がファイル全体の先頭(放送TSでは多くの場合音声)から何秒遅れて
        始まるか。mpvのtime-posはファイル全体の先頭が0なので、プレビューの
        フレーム番号をカット処理と同じ「映像の先頭が0」にそろえるのに使う。"""
        data = self.probe(Path(file_path))
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        try:
            video_start = float(video_stream.get("start_time")) if video_stream else None
            format_start = float(data.get("format", {}).get("start_time"))
        except (TypeError, ValueError):
            return 0.0
        if video_start is None:
            return 0.0
        return max(video_start - format_start, 0.0)

    def get_source_timing(self, file_path: Path) -> SourceTiming:
        """カット処理用に、開始時刻・インターレース・音声パケット長を調べる。"""
        file_path = Path(file_path)
        data = self.probe(file_path)
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video_stream is None:
            raise FFprobeError("E102", key="exceptions.no_video_stream", path=file_path)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        def _float(value) -> Optional[float]:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        video_start = _float(video_stream.get("start_time")) or 0.0
        format_start = _float(data.get("format", {}).get("start_time"))
        if format_start is None:
            format_start = video_start

        audio_packet_duration = 0.0
        if has_audio:
            result = subprocess.run(
                [
                    str(self.ffprobe_path), "-v", "quiet",
                    "-select_streams", "a:0", "-read_intervals", "%+#8",
                    "-show_entries", "packet=duration_time", "-of", "csv=p=0",
                    str(file_path),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                **no_window(),
            )
            durations = [_float(line.strip().rstrip(",")) for line in result.stdout.splitlines()]
            audio_packet_duration = max((d for d in durations if d), default=0.0)

        field_order = video_stream.get("field_order", "unknown")
        duration = _float(data.get("format", {}).get("duration")) or 0.0
        field_based, top_field_first = self._sample_scan_type(file_path, format_start, duration)
        if field_order in ("bb", "bt"):
            top_field_first = False
        elif field_order in ("tt", "tb"):
            top_field_first = True

        return SourceTiming(
            video_start=video_start,
            format_start=format_start,
            field_order=field_order,
            has_audio=has_audio,
            audio_packet_duration=audio_packet_duration,
            has_b_frames=int(video_stream.get("has_b_frames") or 0) > 0,
            field_based=field_based,
            top_field_first=top_field_first,
        )

    def _sample_frames(self, file_path: Path, format_start: float, duration: float) -> List[dict]:
        """録画の数か所(5/35/65/90%)から2秒ずつ、フレームの走査情報を読む。"""
        points = [format_start + duration * r for r in (0.05, 0.35, 0.65, 0.9)] if duration > 0 else [format_start]
        intervals = ",".join(f"{p:.3f}%+2" for p in points)
        result = subprocess.run(
            [
                str(self.ffprobe_path), "-v", "quiet", "-select_streams", "v:0",
                "-read_intervals", intervals,
                "-show_entries", "frame=interlaced_frame,top_field_first,repeat_pict",
                "-of", "json", str(file_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **no_window(),
        )
        try:
            return json.loads(result.stdout or "{}").get("frames", [])
        except json.JSONDecodeError:
            return []

    def _sample_scan_type(self, file_path: Path, format_start: float, duration: float):
        """録画の数か所から2秒ずつフレームを調べ、(フィールド単位の映像か,
        トップフィールドが先か)を返す。CS-Aの実録画では、本編中でソフト
        テレシネ(プログレッシブ+フィールド繰り返し)とインターレースが
        頻繁に切り替わっていた(2026-10-04)。"""
        frames = self._sample_frames(file_path, format_start, duration)
        field_frames = [
            f for f in frames if f.get("interlaced_frame") == 1 or f.get("repeat_pict", 0) > 0
        ]
        interlaced = [f for f in frames if f.get("interlaced_frame") == 1]
        tff_votes = sum(1 for f in interlaced if f.get("top_field_first") == 1)
        top_field_first = tff_votes * 2 >= len(interlaced) if interlaced else True
        return bool(field_frames), top_field_first

    def get_scan_type(self, file_path: Path) -> str:
        """走査方式を SCAN_PROGRESSIVE / SCAN_INTERLACED / SCAN_SOFT_TELECINE /
        SCAN_MIXED のどれかで返す(画面表示用。録画の数か所を調べる)。"""
        fmt = self.probe(Path(file_path)).get("format", {})
        try:
            format_start = float(fmt.get("start_time") or 0.0)
            duration = float(fmt.get("duration") or 0.0)
        except ValueError:
            format_start, duration = 0.0, 0.0
        frames = self._sample_frames(Path(file_path), format_start, duration)
        interlaced = any(f.get("interlaced_frame") == 1 for f in frames)
        telecine = any(f.get("interlaced_frame") != 1 and f.get("repeat_pict", 0) > 0 for f in frames)
        if interlaced and telecine:
            return SCAN_MIXED
        if telecine:
            return SCAN_SOFT_TELECINE
        return SCAN_INTERLACED if interlaced else SCAN_PROGRESSIVE

    def _read_video_packets(self, file_path: Path, start: float, end: float) -> List[Tuple[float, float, bool]]:
        """[start, end](絶対時刻)付近の映像パケットの(表示時刻, 長さ, キーフレームか)を
        ファイル内の順(=デコード順)に返す。表示時刻の無いパケットは数えない。"""
        result = subprocess.run(
            [
                str(self.ffprobe_path), "-v", "quiet", "-select_streams", "v:0",
                "-read_intervals", f"{max(0.0, start):.6f}%{end:.6f}",
                "-show_entries", "packet=pts_time,duration_time,flags", "-of", "csv=p=0",
                str(file_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **no_window(),
        )
        if result.returncode != 0:
            raise FFprobeError("E102", result.stderr)
        packets = []
        for line in result.stdout.splitlines():
            fields = line.strip().rstrip(",").split(",")
            try:
                pts = float(fields[0])
            except (ValueError, IndexError):
                continue
            try:
                duration = float(fields[1])
            except (ValueError, IndexError):
                duration = 0.0
            packets.append((pts, duration, len(fields) > 2 and "K" in fields[2]))
        return packets

    def get_video_packet_times(
        self, file_path: Path, start: float, end: float, fps: Optional[float] = None
    ) -> List[Tuple[float, float]]:
        """[start, end](絶対時刻)付近の映像のフレームの(表示時刻, 長さ)を
        表示時刻順に返す。デコードしないので速い。fps を渡すと、フィールドごとの
        パケット(merge_field_pictures)を 1 フレームにまとめる。"""
        packets = self._read_video_packets(file_path, start, end)
        if fps:
            packets = merge_field_pictures(packets, fps)
        return sorted((pts, duration) for pts, duration, _ in packets)

    def get_video_decode_order(
        self, file_path: Path, read_intervals: Optional[str] = None
    ) -> Tuple[Optional[float], List[Tuple[float, Optional[float]]]]:
        """映像の(start_time, [(表示時刻, デコード時刻), ...])を返す。パケットは
        ファイル内の順(=デコード順)。read_intervalsで読む範囲を絞れる。"""
        cmd = [str(self.ffprobe_path), "-v", "quiet", "-select_streams", "v:0"]
        if read_intervals:
            cmd += ["-read_intervals", read_intervals]
        cmd += ["-show_entries", "stream=start_time:packet=pts_time,dts_time", "-of", "json", str(file_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **no_window())
        if result.returncode != 0:
            raise FFprobeError("E102", result.stderr)
        data = json.loads(result.stdout or "{}")

        def _float(value) -> Optional[float]:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        streams = data.get("streams", [])
        start = _float(streams[0].get("start_time")) if streams else None
        packets = []
        for packet in data.get("packets", []):
            pts = _float(packet.get("pts_time"))
            if pts is not None:
                packets.append((pts, _float(packet.get("dts_time"))))
        return start, packets

    def get_first_video_frame_pts(self, file_path: Path, seconds: float = 15.0) -> Optional[float]:
        """デコードできる最初のフレームの表示時刻(絶対時刻)。シークせずファイルの
        先頭から読む(TSのシークは行き過ぎるため、先頭への指定でも先のキー
        フレームから読み始めることがある)。"""
        result = subprocess.run(
            [
                str(self.ffprobe_path), "-v", "quiet", "-select_streams", "v:0",
                "-read_intervals", f"%+{seconds:g}",
                "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(file_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **no_window(),
        )
        times = []
        for line in result.stdout.splitlines():
            try:
                times.append(float(line.strip().rstrip(",")))
            except ValueError:
                continue
        return min(times) if times else None

    def get_video_frames(self, file_path: Path, start: float, end: float) -> List[VideoFrameInfo]:
        """[start, end](絶対時刻)付近のフレームをデコードし、表示時刻と走査
        情報(プログレッシブか、トップフィールドが先か、フィールドを繰り返すか)
        を表示時刻順に返す。"""
        result = subprocess.run(
            [
                str(self.ffprobe_path), "-v", "quiet", "-select_streams", "v:0",
                "-read_intervals", f"{max(0.0, start):.6f}%{end:.6f}",
                "-show_entries", "frame=pts_time,interlaced_frame,top_field_first,repeat_pict",
                "-of", "json", str(file_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **no_window(),
        )
        if result.returncode != 0:
            raise FFprobeError("E102", result.stderr)
        frames = []
        for frame in json.loads(result.stdout or "{}").get("frames", []):
            try:
                pts = float(frame["pts_time"])
            except (KeyError, ValueError):
                continue
            frames.append(VideoFrameInfo(
                pts=pts,
                progressive=frame.get("interlaced_frame", 0) == 0,
                top_field_first=frame.get("top_field_first", 0) == 1,
                repeat_first_field=frame.get("repeat_pict", 0) > 0,
            ))
        return sorted(frames, key=lambda f: f.pts)

    def get_leading_pictures(self, file_path: Path, keyframe_pts: float, fps: float) -> List[float]:
        """キーフレーム(表示時刻keyframe_pts、絶対時刻)の「リーディング
        ピクチャ」の表示時刻(絶対)を返す。オープンGOPでは、キーフレームの
        直前に表示されるBフレームがキーフレームの後ろに格納され、前後両方の
        GOPを参照する。これらはキーフレーム位置で区切ったcopyでは正しく
        含められないため、呼び出し側は直前の区間をその分だけ再エンコードに
        回す必要がある。フレームの長さが一定とは限らない(ソフトテレシネの
        1.5フレーム表示等)ため、枚数ではなく時刻で返す。"""
        # keyframe_ptsはフレーム番号から求めた時刻で、実際のptsとは最大半
        # フレームずれうる(ソフトテレシネでは半フレーム位置にフレームがある)。
        tolerance = 0.75 / fps
        # フィールドごとのパケットは 1 フレームにまとめる(キーフレームの 2 つ目のフィールドは
        # キーフレームより後に表示されるので、そこで探すのをやめてしまう)
        packets = merge_field_pictures(
            self._read_video_packets(file_path, keyframe_pts - 2.0, keyframe_pts + 2.0), fps
        )

        for index, (pts, _, key) in enumerate(packets):
            if key and abs(pts - keyframe_pts) <= tolerance:
                leading = []
                for following_pts, _, _ in packets[index + 1:]:
                    if following_pts >= pts:
                        break
                    leading.append(following_pts)
                return leading
        return []

    def is_vfr(self, file_path: Path) -> bool:
        """avg_frame_rateとr_frame_rateを比較しVFR(可変フレームレート)か
        判定する (design doc §16.1)。VFRの場合Smart Cutは呼び出し側
        (core/エンジン層)で自動無効化される想定。"""
        data = self.probe(Path(file_path))
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video_stream is None:
            raise FFprobeError("E102", key="exceptions.no_video_stream", path=file_path)

        return _is_vfr_from_rates(
            video_stream.get("avg_frame_rate", "0/1"),
            video_stream.get("r_frame_rate", "0/1"),
        )

    def get_stream_packets(self, file_path: Path, stream_indexes: List[int],
                           probe_args: List[str] = (), frame_rates: Optional[dict] = None) -> dict:
        """録画全体を 1 回読み、stream_indexes の各ストリームのパケットの (表示時刻, 長さ) を表示の順に
        返す({ストリームの番号: [...]}、絶対時刻、秒)。表示時刻の無いパケットは数えない。
        frame_rates({ストリームの番号: fps})のストリームは、フィールドごとのパケットを 1 フレームにまとめる
        (merge_field_pictures。L-SMASH Works はフィールドの絵 2 枚を 1 フレームと数えた、2026-11-17)。

        スクリプトの出力(設計書 §6.9)で、残す区間を「録画の中で表示の順に何番目の絵か」と、音声の
        何サンプル目かに直すのに使う(L-SMASH Works の数え方と同じ。試作で確かめた)。録画全体を読むので、
        中止(cancel_event)で止められる。"""
        cmd = [
            str(self.ffprobe_path), "-v", "quiet", *probe_args,
            "-show_entries", "packet=stream_index,pts_time,duration_time", "-of", "csv=p=0", str(file_path),
        ]
        try:
            result = run_until_stalled(cmd, STALL_SECONDS, cancel_event=self.cancel_event)
        except StalledProcessError as e:
            raise FFprobeError("E102", key="exceptions.keyframes_stalled", seconds=f"{e.seconds:.0f}",
                               path=file_path) from e
        if self.cancel_event.is_set():
            raise ProcessingCancelledError("E504", key="exceptions.cancelled")
        if result.returncode != 0:
            raise FFprobeError("E102", result.stderr.decode("utf-8", errors="replace"))
        packets: dict = {index: [] for index in stream_indexes}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.strip().rstrip(",").split(",")
            try:
                target = packets.get(int(fields[0]))
                if target is None:
                    continue
                pts = float(fields[1])
            except (ValueError, IndexError):
                continue  # N/A(表示時刻の無いパケット)
            try:
                duration = float(fields[2])
            except (ValueError, IndexError):
                duration = 0.0
            target.append((pts, duration))
        for index, fps in (frame_rates or {}).items():
            if index in packets:
                frames = merge_field_pictures([(pts, duration, False) for pts, duration in packets[index]], fps)
                packets[index] = [(pts, duration) for pts, duration, _ in frames]
        for values in packets.values():
            values.sort()
        return packets

    # ---- キーフレーム -------------------------------------------------

    def get_keyframes(
        self,
        file_path: Path,
        fps: Optional[float] = None,
        use_cache: bool = True,
    ) -> List[int]:
        """キーフレーム位置をフレーム番号のリストで返す。名前・サイズ・mtime で見分けるファイルの
        キャッシュを使う(30 日間使われなかったものは起動時に消す)。"""
        file_path = Path(file_path)

        if use_cache:
            cached = _load_keyframe_cache(file_path)
            if cached is not None:
                return cached

        if fps is None:
            fps = self.get_file_info(file_path).fps

        # TSコンテナ等ではPTSが0秒から始まらないことがある(実機検証:
        # MPEG-2/TSでPTSが1.44秒から始まるケースを確認済み)。ffprobeの
        # frame=pts_timeは生のPTSをそのまま返すため、0秒始まりの前提で
        # フレーム番号へ変換するとキーフレーム位置がその分ズレる。
        # 映像ストリームのstart_timeを基準点として差し引く。
        probe_data = self.probe(file_path)
        probe_video_stream = next(
            (s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"),
            None,
        )
        start_time_raw = probe_video_stream.get("start_time") if probe_video_stream else None
        try:
            start_time = float(start_time_raw) if start_time_raw is not None else 0.0
        except ValueError:
            start_time = 0.0

        cmd = [
            str(self.ffprobe_path),
            "-v", "quiet",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time",
            "-of", "csv=p=0",
            str(file_path),
        ]
        try:
            result = run_until_stalled(cmd, STALL_SECONDS)
        except StalledProcessError as e:
            raise FFprobeError("E102", key="exceptions.keyframes_stalled", seconds=f"{e.seconds:.0f}",
                               path=file_path) from e
        if result.returncode != 0:
            raise FFprobeError("E102", result.stderr.decode("utf-8", errors="replace"))

        frames = set()
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip().rstrip(",")
            if not line or line == "N/A":
                continue
            try:
                pts_time = float(line)
            except ValueError:
                continue
            frames.add(time_to_frame(max(0.0, pts_time - start_time), fps))

        keyframes = sorted(frames)

        if use_cache:
            _save_keyframe_cache(file_path, keyframes)

        return keyframes
