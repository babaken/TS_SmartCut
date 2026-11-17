"""Smart Cutエンジン (design doc §6.1-6.3, §6.6-6.7)。

CutRangeは「削除する区間」を表す。エンジンはその補集合である「残す区間」
を求め、モードに応じて出力ピースを計画・生成し、concatデムクサで結合する。

- Smart Cut: 各残す区間をHead(再エンコード)/Body(-c copy)/Tail(再エンコード)
  に分割し、GOP境界の内側だけをストリームコピーする。Head/Tailはソースと
  同じ映像コーデック(MPEG-2/H.264(AVC)のいずれか)で再エンコードする —
  Bodyがソースコーデックのままconcat -c copyされるため、異なるコーデックを
  混在させると出力が壊れる(実機検証で確認済み)。対応外コーデックは
  UnsupportedCodecError(E302)を送出する。
- Simple Copy: 各残す区間の開始位置を直後のキーフレームへスナップして
  丸ごとcopyする（フレーム精度は犠牲になるが最速）。
- Full Re-encode: 各残す区間をそのまま再エンコードする。

再エンコードのコーデック・走査方式(インターレース/ソフトテレシネ)・画質
(ビットレート)は常に元ソースに合わせる。このツールの目的はSmart Cutで、
コーデックやコンテナの変換は目的外(利用者の要件、2026-10-05)。

VFRソースは resolve_processing_mode() によりSmart Cutが自動的に
Simple Copyへ強制される（design doc §16.2、CLAUDE.md記載の中核方針）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from ts_smartcut.errors import MuxFailedError, UnsupportedCodecError, ValidationError
from ts_smartcut.ffmpeg.compat import bytes_per_frame, x264_rate_args
from ts_smartcut.ffmpeg.gpu import (
    HardwareDetection,
    choose_encoder,
    choose_hardware,
    hw_filter,
    hw_input_args,
    quality_args,
)
from ts_smartcut.ffmpeg.mpeg2_flags import (
    PictureFlags,
    count_pictures,
    frame_rate_code,
    rewrite_field_flags,
)
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper, SourceTiming
from ts_smartcut.i18n.language_manager import english
from ts_smartcut.models import AppSettings, CutRange, FileCutInfo
from ts_smartcut.system.temp_file_manager import TempFileManager
from ts_smartcut.time_utils import frame_to_time

logger = logging.getLogger(__name__)

# 処理モード (design doc §6.1)
MODE_SMART_CUT = "smart_cut"
MODE_SIMPLE_COPY = "simple_copy"
MODE_FULL_REENCODE = "full_reencode"
PROCESSING_MODES = (MODE_SMART_CUT, MODE_SIMPLE_COPY, MODE_FULL_REENCODE)

_VALID_MODES = (MODE_SMART_CUT, MODE_SIMPLE_COPY, MODE_FULL_REENCODE)

# x264の速度設定(画質はビットレートで元ソースに合わせるので、速さだけの設定)
_X264_PRESET = "veryfast"

# 元ソースのビットレートが分からない場合の固定品質(CRF相当/MPEG-2はqscale)
_FALLBACK_QUALITY = 18

_MPEGTS_SUFFIXES = {".ts", ".m2ts", ".mts"}

# Smart CutのTailの最小フレーム数(plan_smart_cut_parts)。Bフレーム2枚の
# MPEG-2では4フレーム以下でデコード時刻が1フィールド未満に詰まり、5フレームで
# 足りた(実測)。CS-Aの実録画(ソフトテレシネ混在)では6・7フレームのTailで
# 問題なかった。
MIN_TAIL_FRAMES = 6
# コピー部分の最後のデコード時刻を読む時に、末尾から読む長さ(秒)。これ以下の
# 部分はシークせず全体を読む(_fit_decode_times)
_TAIL_READ_SECONDS = 3.0

# Smart CutのHead/Tailは、BodyがConcatで-c copyされる都合上、Bodyと同じ
# コーデックで再エンコードしなければならない(異なるコーデックを混在させて
# concatすると出力が壊れることを実機検証で確認した — 2026-08-22)。
# 日本のテレビ放送のTSはMPEG-2かH.264(AVC)。ffprobeの生コーデック名 ->
# gpu.pyと共通のfamily tag ("h264"/"mpeg2")。
_SOURCE_CODEC_TO_FAMILY = {"h264": "h264", "mpeg2video": "mpeg2"}
_FAMILY_TO_SW_ENCODER = {"h264": "libx264", "mpeg2": "mpeg2video"}
_SW_ENCODER_TO_FAMILY = {v: k for k, v in _FAMILY_TO_SW_ENCODER.items()}


class _LazyLeadingPictures:
    """plan_smart_cut_partsのleading_pictures用。キーフレームごとのリーディング
    ピクチャ数を、初めて聞かれた時に調べて覚える(ffprobeを1回ずつ走らせるため)。"""

    def __init__(self, probe: Callable[[int], int]):
        self._probe = probe
        self._cache: Dict[int, int] = {}

    def get(self, keyframe: int, default: int = 0) -> int:
        if keyframe not in self._cache:
            self._cache[keyframe] = self._probe(keyframe)
        return self._cache[keyframe]


class SegmentPart(NamedTuple):
    """出力ピース1つ分。start/endはソースのフレーム番号（endは排他的）。"""

    action: str  # "copy" or "reencode" -- ffmpegに何をさせるか
    phase: str  # "Head" / "Body" / "Tail" / "Copy" / "Reencode" -- 進捗表示用
    start_frame: int
    end_frame: int


@dataclass
class ProcessingProgressEvent:
    phase: str
    part_index: int
    total_parts: int
    phase_progress: float  # 0.0-1.0 このパート内での進捗
    # いま処理している元ファイルのフレーム番号(プレビュー・シークバーを処理に
    # 合わせて動かす用)。音声の切り出しのように映像を伴わない段階ではNone。
    source_frame: Optional[int] = None


ProgressCallback = Callable[[ProcessingProgressEvent], None]


def resolve_processing_mode(requested_mode: str, is_vfr: bool) -> Tuple[str, Optional[str]]:
    """VFRソースの場合Smart CutをSimple Copyへ自動的に切り替える
    (design doc §16.2)。戻り値は (実際に使うモード, 警告メッセージ or None)。"""
    if requested_mode == MODE_SMART_CUT and is_vfr:
        # ログに出す文(英語)。画面は main.vfr_warning を出す
        return MODE_SIMPLE_COPY, (
            "Variable frame rate (VFR): Smart Cut is disabled and the file is processed in Simple Copy mode; "
            "cut points are moved to keyframe boundaries."
        )
    return requested_mode, None


def compute_keep_segments(total_frames: int, cuts: List[CutRange]) -> List[Tuple[int, int]]:
    """cuts（削除する区間）の補集合として残す区間を求める。
    範囲外・重複したcutsも(clamp/merge して)安全に扱う。"""
    if total_frames <= 0:
        return []

    sorted_cuts = sorted(cuts, key=lambda c: c.start_frame)
    segments: List[Tuple[int, int]] = []
    cursor = 0
    for cut in sorted_cuts:
        start = max(0, min(cut.start_frame, total_frames))
        end = max(0, min(cut.end_frame, total_frames))
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_frames:
        segments.append((cursor, total_frames))
    return segments


def plan_smart_cut_parts(
    keep_segments: List[Tuple[int, int]],
    keyframes: List[int],
    total_frames: Optional[int] = None,
    leading_pictures: Optional[Dict[int, int]] = None,
) -> List[SegmentPart]:
    """各残す区間をHead/Body/Tailに分割する (design doc §6.3)。
    区間内にGOP境界が無い短い区間は1本のreencodeにまとめる。

    残す区間が動画の末尾(total_frames)まで続く場合、Tailは作らずBodyを
    末尾までcopyする。末尾の後ろには連結する相手がいないため、最後の
    キーフレームで区切って再エンコードする必要がない（無駄な劣化になる）。

    leading_pictures: {キーフレーム: リーディングピクチャ数}(.get()で引ければよい)。
    オープンGOPではキーフレーム直前に表示されるBフレームがキーフレームの後ろに
    格納され、前後のGOPを両方参照するため、Bodyのcopyに含められない。その分だけ
    Bodyを短くし、Tailで再エンコードする。

    TailがMIN_TAIL_FRAMES未満になる場合は、1つ前のキーフレームでBodyを区切る。
    copyされたBodyに挟まれた短いTailは、デコード時刻を前後とつなぐ余地が無く
    (_fit_decode_times)、キーフレームちょうどで終わる区間ではリーディング
    ピクチャ2枚だけのTailになり、デコード時刻がほぼ同時刻に詰まっていた
    (2026-10-05、BS-Aの録画とテスト素材で確認)。"""
    leading_pictures = leading_pictures if leading_pictures is not None else {}
    sorted_kf = sorted(keyframes)
    parts: List[SegmentPart] = []

    for seg_start, seg_end in keep_segments:
        if seg_start >= seg_end:
            continue

        kf_start = next((k for k in sorted_kf if k >= seg_start), None)
        reaches_eof = total_frames is not None and seg_end >= total_frames
        if reaches_eof and kf_start is not None and kf_start < seg_end:
            kf_end = seg_end
        else:
            kf_end = None
            for k in sorted_kf:
                if k <= seg_end:
                    kf_end = k
                else:
                    break

        if kf_end is not None and not (reaches_eof and kf_end == seg_end):
            keyframe = kf_end
            kf_end = keyframe - leading_pictures.get(keyframe, 0)
            while kf_start is not None and 0 < seg_end - kf_end < MIN_TAIL_FRAMES:
                earlier = max((k for k in sorted_kf if k < keyframe), default=None)
                if earlier is None or earlier <= kf_start:
                    kf_end = kf_start  # Bodyを作れない → 区間全体を再エンコード
                    break
                keyframe = earlier
                kf_end = keyframe - leading_pictures.get(keyframe, 0)

        if kf_start is None or kf_end is None or kf_start >= kf_end:
            parts.append(SegmentPart("reencode", "Reencode", seg_start, seg_end))
            continue

        if seg_start < kf_start:
            parts.append(SegmentPart("reencode", "Head", seg_start, kf_start))
        parts.append(SegmentPart("copy", "Body", kf_start, kf_end))
        if kf_end < seg_end:
            parts.append(SegmentPart("reencode", "Tail", kf_end, seg_end))

    return parts


def plan_simple_copy_parts(
    keep_segments: List[Tuple[int, int]],
    keyframes: List[int],
    total_frames: Optional[int] = None,
    snap_end: bool = False,
    leading_pictures: Optional[Dict[int, int]] = None,
) -> List[SegmentPart]:
    """各残す区間の開始位置を直後のキーフレームへスナップしてcopyする
    (design doc §6.1: 全区間copy、キーフレーム境界)。

    snap_end: 終了位置も直前のキーフレーム(からリーディングピクチャを除いた
    位置)へスナップする。Bフレームを持つソースでは、GOPの途中で切ると
    終端付近のBフレームが区間外のフレームを参照していて壊れるため
    (2026-10-04 実測)。動画の末尾まで続く区間はスナップしない。"""
    sorted_kf = sorted(keyframes)
    leading_pictures = leading_pictures or {}
    parts: List[SegmentPart] = []
    for seg_start, seg_end in keep_segments:
        kf_start = next((k for k in sorted_kf if k >= seg_start), None)
        if kf_start is None or kf_start >= seg_end:
            continue  # 区間内にキーフレームが無い -> 出力なし
        end = seg_end
        reaches_eof = total_frames is not None and seg_end >= total_frames
        if snap_end and not reaches_eof:
            kf_end = max((k for k in sorted_kf if k <= seg_end), default=kf_start)
            end = kf_end - leading_pictures.get(kf_end, 0)
        if end <= kf_start:
            continue  # 安全にcopyできるフレームが無い
        parts.append(SegmentPart("copy", "Copy", kf_start, end))
    return parts


def plan_full_reencode_parts(keep_segments: List[Tuple[int, int]]) -> List[SegmentPart]:
    """各残す区間をそのまま再エンコードする (design doc §6.1)。"""
    return [
        SegmentPart("reencode", "Reencode", s, e) for s, e in keep_segments if s < e
    ]


# 粗いシーク位置を目標より最低これだけ手前のキーフレームに取る(秒)。
# TSの入力シーク(-ss を -i の前に置く)はインデックスが無く不正確で、
# 目標がキーフレームでないと「次の」キーフレームまで行き過ぎる(実測)。
# 手前で粗くシークしてから出力側の-ssで正確に切り出す。音声はTS内で
# 映像と多少前後して格納されるため、その分の余裕も兼ねる。
_COARSE_SEEK_MARGIN_SEC = 2.0


def coarse_seek_frame(needed_frame: int, keyframes: List[int], fps: float) -> int:
    """needed_frameより十分手前のキーフレームを返す（無ければ0=先頭）。
    行き過ぎても次のキーフレームで止まるよう、余裕より手前の
    キーフレームのうち「最後から2番目」を使う。"""
    limit = needed_frame - _COARSE_SEEK_MARGIN_SEC * fps
    candidates = [k for k in sorted(keyframes) if k <= limit]
    return candidates[-2] if len(candidates) >= 2 else 0


def contiguous_runs(parts: List[SegmentPart]) -> List[Tuple[int, int]]:
    """出力ピースのうちソース上で連続しているものをまとめた区間の一覧。
    音声はこの単位で切り出す（Head/Body/Tailの境目では切らない）。"""
    runs: List[Tuple[int, int]] = []
    for part in parts:
        if runs and runs[-1][1] == part.start_frame:
            runs[-1] = (runs[-1][0], part.end_frame)
        else:
            runs.append((part.start_frame, part.end_frame))
    return runs


# 選んだ音声が最初の音声でない時の読み取り幅(設計書 §4.12)。番組が始まってから PMT に加わる音声
# (GR-B の 2 つ目)は、既定の幅(5MB)では形式が分からず取り出しが失敗した
_AUDIO_PROBE_ARGS = ["-analyzeduration", "30M", "-probesize", "100M"]


def _bytes_per_frame(file_info: FileCutInfo) -> Optional[float]:
    """frame= を出さない ffmpeg(6.x のコピー)の進み具合の見積もりに使う大きさ(compat.py)。"""
    return bytes_per_frame(file_info.bitrate, file_info.fps)


def audio_input(file_info: FileCutInfo) -> Tuple[List[str], str]:
    """音声を切り出す ffmpeg の (読み取りの指定, -map)。出力に入れるのは選んだ音声だけ(§4.12)。
    最初の音声(何も選んでいない時も)は今までどおり 0:a:0、ほかは PID で選び読み取り幅を広げる。"""
    track = file_info.audio_track
    if not track or int(track.get("index", 0)) == 0:
        return [], "0:a:0"
    return list(_AUDIO_PROBE_ARGS), f"0:i:{int(track['pid']):#x}"


class SmartCutEngine:
    """映像と音声を分けて処理し、最後に1つへまとめる。

    以前は各ピースを映像+音声(-c:a copy)で書き出して連結していたが、
    入力シーク時にコピーした音声だけが指定位置より手前から始まり、
    (1) カット区間(CM)の音声が混入する (2) ピース同士で音声が重複する
    (3) 連結時に音声側の長さで並ぶため映像の時刻に空きができる、という
    不具合があった(2026-10-04 実測)。映像ピースは映像のみで作り、音声は
    連続区間ごとに切り出し、両方を映像の正確な長さで並べて連結する。"""

    def __init__(
        self,
        ffmpeg: FFmpegWrapper,
        temp_manager: TempFileManager,
        settings: AppSettings,
        hardware: Optional[HardwareDetection] = None,
    ):
        self.ffmpeg = ffmpeg
        self.temp_manager = temp_manager
        self.settings = settings
        # 初回起動時のハードウェアエンコーダー判定結果(core/hardware_encoders.py)。
        # Noneならソフトウェアエンコーダーのみ。
        self.hardware = hardware
        self._encoder_cache: Dict[str, str] = {}
        self._encoder_devices: Dict[str, Optional[str]] = {}
        # MPEG-2のフィールド構造を保つ符号化で、GPUの出力が使えなかったか(このファイルの間は
        # 以降ソフトウェアにする)
        self._mpeg2_gpu_failed = False
        self._timing: Optional[SourceTiming] = None
        self._keyframes: List[int] = []
        # フレーム番号 -> そのフレームから始まる区間の、実際の最初のフレームの表示時刻
        self._boundaries: Dict[int, float] = {}
        self._video_end: Optional[float] = None

    def process(
        self,
        file_info: FileCutInfo,
        cuts: List[CutRange],
        output_path: Path,
        mode: str = MODE_SMART_CUT,
        audio_codec: str = "copy",
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Tuple[str, Optional[str]]:
        """カット済み動画をoutput_pathへ出力する。cutsは削除する区間
        (FileCutInfo.cutsは残す区間なので、呼び出し側がmodels.removed_rangesで
        変換して渡す)。戻り値は (実際に使用したモード, VFR強制時の警告メッセージ or None)。"""
        if mode not in _VALID_MODES:
            raise ValidationError("E201", key="exceptions.invalid_mode", mode=mode)

        if mode != MODE_SIMPLE_COPY and file_info.video_codec not in _SOURCE_CODEC_TO_FAMILY:
            raise UnsupportedCodecError("E302", key="exceptions.unsupported_codec", codec=file_info.video_codec)

        is_vfr = self.ffmpeg.is_vfr(file_info.file_path) if mode == MODE_SMART_CUT else False
        effective_mode, warning = resolve_processing_mode(mode, is_vfr)

        # 再エンコードは常に元ソースと同じコーデック。Smart CutではBodyがconcatで
        # 元のままcopyされるため、異なるコーデックを混ぜると出力が壊れる。
        family = _SOURCE_CODEC_TO_FAMILY.get(file_info.video_codec)
        video_codec = _FAMILY_TO_SW_ENCODER[family] if family else None

        # GPUの動作確認(1フレームの試しエンコード)はパートごとに繰り返さない。
        self._encoder_cache = {}
        self._encoder_devices = {}
        self._boundaries = {}
        self._mpeg2_gpu_failed = False
        self._video_end = None
        self._timing = self.ffmpeg.get_source_timing(file_info.file_path)
        # キーフレームは全モードでシーク位置の決定に使う（キャッシュ済みなら即時）。
        self._keyframes = self.ffmpeg.get_keyframes(file_info.file_path, fps=file_info.fps)
        # total_framesはnb_framesの無いTSでは全体の長さからの見積もりで、音声が
        # 映像より長いと実際より多い(BS-Aの実録画: 86521 / 実際86514)。末尾を残す
        # 区間が存在しないフレームを指し、再エンコードでE101になっていたため、
        # 実際の最後のフレームまでに合わせる。
        actual_frames = math.floor(
            (self._end_of_video(file_info) - self._timing.video_start) * file_info.fps + 0.5
        )
        if 0 < actual_frames < file_info.total_frames:
            file_info = replace(file_info, total_frames=actual_frames)

        keep_segments = compute_keep_segments(file_info.total_frames, cuts)
        # 録画はGOPの途中から始まることが多く、最初のキーフレームより前のフレームは
        # デコードできない(BS-Aの実録画: 先頭17フレーム)。先頭を残す区間がそこから
        # 始まると、Headの再エンコードで元フレームが見つからずE101で止まっていた。
        first = self._first_decodable_frame(file_info)
        keep_segments = [(max(s, first), e) for s, e in keep_segments if max(s, first) < e]
        if effective_mode == MODE_SMART_CUT:
            # Tailが短すぎる時に1つ前のキーフレームで区切るため、どのキーフレームの
            # リーディングピクチャが必要かは分割しながら決まる → 必要な時に調べる。
            parts = plan_smart_cut_parts(
                keep_segments, self._keyframes, file_info.total_frames,
                _LazyLeadingPictures(lambda k: self._leading_pictures_at(file_info, k)),
            )
        elif effective_mode == MODE_SIMPLE_COPY:
            snap_end = self._timing.has_b_frames
            parts = plan_simple_copy_parts(
                keep_segments, self._keyframes, file_info.total_frames, snap_end
            )
            if snap_end:
                leading = self._probe_leading_pictures(file_info, parts)
                if any(leading.values()):
                    parts = plan_simple_copy_parts(
                        keep_segments, self._keyframes, file_info.total_frames, snap_end, leading
                    )
        else:  # MODE_FULL_REENCODE
            parts = plan_full_reencode_parts(keep_segments)

        if not parts:
            raise ValidationError("E201", key="exceptions.nothing_to_output")

        # 映像ピースの一時ファイルは常にMPEG-TSで作る。TSはH.264/HEVCの
        # SPS/PPSをストリーム内に持つため、再エンコードしたHead/Tailと元の
        # Bodyでパラメータが異なっても連結後に正しくデコードできる(MP4/MKVは
        # 先頭ファイルのものしか保持しない)。最終出力のコンテナ(拡張子)へは
        # _join()で必ず詰め替えるので、拡張子と中身がずれることもない。
        part_suffix = ".ts"

        smart_cut = effective_mode == MODE_SMART_CUT

        audio_runs = contiguous_runs(parts) if self._timing.has_audio else []
        total_steps = len(parts) + len(audio_runs) + 1

        try:
            video_files = []
            for index, part in enumerate(parts):
                part_path = self.temp_manager.create_temp_file(suffix=part_suffix)
                self._render_part(
                    file_info, part, part_path, video_codec, audio_codec,
                    index, total_steps, progress_callback, smart_cut,
                )
                video_files.append((part_path, self._span(file_info, part.start_frame, part.end_frame)))

            try:
                audio_files = self._render_audio_runs(
                    file_info, audio_runs, ".mka", audio_codec, len(parts), total_steps, progress_callback,
                )
            except MuxFailedError:
                # 受信エラーで中身(ADTSヘッダー)が壊れたAACパケットは、分離器が壊れていると
                # 印を付けないことがあり(+discardcorruptで捨てられない)、Matroskaへ書く時に
                # 自動で通るaac_adtstoascがそこで止まった(CS-Bの_drop録画4本、
                # 2026-10-10)。NUTはパケットをそのまま書くので止まらない。ふつうの録画は
                # Matroskaのまま。部品ごとに形式が混ざると連結できないので、この録画の音声を
                # すべてNUTで書き直す。
                logger.warning("Could not write the audio as Matroska, writing it as NUT instead: %s", file_info.file_path.name)
                audio_files = self._render_audio_runs(
                    file_info, audio_runs, ".nut", audio_codec, len(parts), total_steps, progress_callback,
                )

            self._continue_copied_order(file_info, parts, video_files)
            self._fit_decode_times(file_info, parts, video_files)
            self._join(
                file_info, video_files, audio_files, output_path, total_steps, progress_callback,
                [(part.start_frame, part.end_frame) for part in parts],
            )
        finally:
            self.temp_manager.cleanup()

        return effective_mode, warning

    # ---- エンコーダー選択 ------------------------------------------------

    def _select_video_encoder(self, video_codec: str) -> str:
        """判定済みのGPUが必要なこと(コーデック・インターレースのまま符号化)を
        できればGPU、できなければソフトウェアエンコーダー。"""
        if video_codec in self._encoder_cache:
            return self._encoder_cache[video_codec]
        family = _SW_ENCODER_TO_FAMILY.get(video_codec, "h264")
        interlaced = self._output_interlaced()
        # GPU が使えるかは判定(ffmpeg/gpu.py)で、入っている ffmpeg に実際の処理と同じ指定を試して決めてある
        gpu = choose_hardware(self.hardware, family, interlaced)
        encoder = choose_encoder(self.hardware, family, interlaced)
        if gpu is not None:
            logger.info("Using hardware encoder: %s (%s)", encoder, gpu.label_in(english))
        self._encoder_cache[video_codec] = encoder
        self._encoder_devices[encoder] = gpu.device if gpu else None
        return encoder

    def _source_interlaced(self) -> bool:
        return self._timing is not None and self._timing.interlaced

    def _output_interlaced(self) -> bool:
        """出力(の再エンコード部分)をインターレースで符号化するか(元ソースどおり)。"""
        return self._source_interlaced()

    def _field_filter(self, fps: float) -> Optional[str]:
        """フィールド単位の映像を再エンコードする前に通すフィルタ。

        ソフトテレシネ(24コマのプログレッシブ+「フィールドを繰り返す」指示で
        60フィールドとして表示)は、ffmpegのMPEG-2エンコーダーが繰り返し指示を
        書き出せないため、そのまま再エンコードするとプログレッシブのコマが
        フィールド順の入れ替わるインターレースとして符号化され、表示が乱れる
        (CS-Aの実録画で確認、2026-10-04)。repeatfieldsで実際に表示される
        フィールド列(=テレビの表示と同一)へ展開してから、フィールド順を元の
        録画全体に揃える。通常のインターレースのフレームはそのまま通る。

        repeatfieldsが付ける時刻は不規則(半フレーム間隔のこともあり、MPEG-2
        エンコーダーが「時刻が前と同じ」で失敗した)。出力は1フレーム=2フィー
        ルド=ちょうど1フレーム分なので、アプリ全体で使うフレームの刻み
        (映像の開始時刻からfps間隔)に揃えて等間隔に付け直す。"""
        if not self._source_interlaced():
            return None
        order = "tff" if self._timing.top_field_first else "bff"
        vs = self._timing.video_start
        grid_start = f"{vs:.6f}+round((STARTPTS*TB-{vs:.6f})*{fps:.9f})/{fps:.9f}"
        return f"repeatfields,setfield={order},setpts='({grid_start}+N/{fps:.9f})/TB'"

    # ---- シーク ------------------------------------------------------------
    # 位置指定はすべて-copytsでソースの元のタイムスタンプ(絶対時刻)のまま
    # 扱う。ffmpegの-ssは入力側(-iの前)がファイル先頭基準、出力側(-iの後)が
    # 映像先頭基準と基準が異なり、TSでは音声と映像の開始時刻がずれている
    # ことが多いため、相対時刻で指定すると数フレームずれる(2026-10-04 実測)。

    def _abs_time(self, frame: float, fps: float) -> float:
        """フレーム番号(映像start_time基準)を絶対タイムスタンプ(秒)へ。"""
        video_start = self._timing.video_start if self._timing else 0.0
        return video_start + frame_to_time(frame, fps)

    def _coarse_seek_args(self, needed_frame: int, fps: float) -> List[str]:
        """-iの前に置く粗いシーク(入力側の-ssはファイル先頭基準)。"""
        coarse_frame = coarse_seek_frame(needed_frame, self._keyframes, fps)
        if coarse_frame <= 0:
            return ["-copyts"]
        format_start = self._timing.format_start if self._timing else 0.0
        position = self._abs_time(coarse_frame, fps) - format_start
        return ["-copyts", "-ss", f"{max(0.0, position):.6f}"]

    def _boundary(self, file_info: FileCutInfo, frame: int) -> float:
        """フレーム番号frameから始まる区間の、実際の最初のフレームの表示時刻。

        区間の境界は「表示時刻 >= 映像開始 + (frame-0.5)/fps」で決める
        (アプリ全体のフレーム番号の刻みと同じ)。ただし実際に切る位置・各部分の
        長さ・音声の切り出し位置には、その条件を満たす最初のフレームの実際の
        時刻を使う。ソフトテレシネでは1コマが2または3フィールド分表示され、
        フレーム番号/fpsから計算した時刻とは最大半フレームずれるため。"""
        if frame in self._boundaries:
            return self._boundaries[frame]
        fps = file_info.fps
        if frame >= file_info.total_frames:
            value = self._end_of_video(file_info)
        else:
            threshold = self._abs_time(frame - 0.5, fps)
            packets = self.ffmpeg.get_video_packet_times(
                file_info.file_path, threshold - 3.0, threshold + 3.0, fps=fps
            )
            later = [pts for pts, _ in packets if pts >= threshold - 1e-6]
            value = later[0] if later else self._end_of_video(file_info)
        self._boundaries[frame] = value
        return value

    def _end_of_video(self, file_info: FileCutInfo) -> float:
        """最後のフレームの表示終了時刻(絶対)。"""
        if self._video_end is None:
            format_start = self._timing.format_start if self._timing else 0.0
            tail_start = format_start + file_info.duration - 6.0
            packets = self.ffmpeg.get_video_packet_times(
                file_info.file_path, tail_start, format_start + file_info.duration + 6.0, fps=file_info.fps
            )
            if packets:
                pts, duration = packets[-1]
                self._video_end = pts + (duration or 1.0 / file_info.fps)
            else:
                self._video_end = self._abs_time(file_info.total_frames, file_info.fps)
        return self._video_end

    def _span(self, file_info: FileCutInfo, start_frame: int, end_frame: int) -> float:
        """[start_frame, end_frame)の実際の表示時間(秒)。"""
        return self._boundary(file_info, end_frame) - self._boundary(file_info, start_frame)

    def _probe_leading_pictures(
        self, file_info: FileCutInfo, parts: List[SegmentPart]
    ) -> Dict[int, int]:
        """Bodyの終端キーフレームごとに、copyに含められないリーディング
        ピクチャの分だけ何フレーム手前で切るべきかを調べる。"""
        leading: Dict[int, int] = {}
        for part in parts:
            if part.action == "copy" and part.end_frame < file_info.total_frames:
                count = self._leading_pictures_at(file_info, part.end_frame)
                if count:
                    leading[part.end_frame] = count
        return leading

    def _first_decodable_frame(self, file_info: FileCutInfo) -> int:
        """デコードできる最初のフレームの番号(フレーム番号は映像の先頭パケットが0)。"""
        if self._timing is None:
            return 0
        pts = self.ffmpeg.get_first_video_frame_pts(file_info.file_path)
        if pts is None:
            return 0
        return max(0, math.floor((pts - self._timing.video_start) * file_info.fps + 0.5))

    def _leading_pictures_at(self, file_info: FileCutInfo, keyframe: int) -> int:
        """キーフレームの直前に表示されるが後ろに格納されたリーディングピクチャの数。"""
        fps = file_info.fps
        video_start = self._timing.video_start if self._timing else 0.0
        pts_list = self.ffmpeg.get_leading_pictures(
            file_info.file_path, self._abs_time(keyframe, fps), fps
        )
        if not pts_list:
            return 0
        # 区切りは「時刻 >= 境界-半フレーム」で判定するため、最初の
        # リーディングピクチャが次の区間側に入る境界を求める。
        first = (min(pts_list) - video_start) * fps
        boundary = math.floor(first + 0.5)
        return max(0, keyframe - boundary)

    # ---- 映像ピース ----------------------------------------------------------

    def _render_part(
        self,
        file_info: FileCutInfo,
        part: SegmentPart,
        out_path: Path,
        video_codec: str,
        audio_codec: str,
        index: int,
        total: int,
        progress_callback: Optional[ProgressCallback],
        smart_cut: bool = False,
    ) -> None:
        """映像のみのピースを書き出す（音声は_render_audioで別に扱う）。"""
        fps = file_info.fps
        pre = self._coarse_seek_args(part.start_frame, fps)
        # 実際のフレームの表示時刻で切る。比較の丸めで境界のフレームを
        # 取りこぼさないよう1ms手前を閾値にする(フレーム間隔より十分小さい)。
        start_pts = self._boundary(file_info, part.start_frame)
        end_pts = self._boundary(file_info, part.end_frame)
        start_abs, end_abs = start_pts - 0.001, end_pts - 0.001

        if part.action == "copy":
            # copyは「表示時刻」が[開始, 終了)のパケットだけを残す。デコード順の
            # パケット数(-frames:v)で切ると、Bフレームの並べ替えのせいで終端の
            # フレームが欠け、開始キーフレームの後ろに格納された前のGOPの
            # Bフレーム(リーディングピクチャ、単独ではデコード不可)が混入する
            # (2026-10-04 実測)。終了側の読み込みは余裕を持たせて打ち切る。
            drop = f"noise=drop=lt(pts*tb\\,{start_abs:.6f})+gte(pts*tb\\,{end_abs:.6f})"
            args = [
                "-y", *pre, "-i", str(file_info.file_path),
                "-map", "0:v:0", "-to", f"{end_abs + 2.0:.6f}",
                "-c", "copy", "-bsf:v", drop,
            ]
        elif self._preserve_mpeg2_fields(video_codec):
            self._render_mpeg2_preserving_fields(
                file_info, part, out_path, pre, start_pts, end_pts,
                index, total, progress_callback,
            )
            return
        else:
            # デコード後のフレームを表示時刻(-copytsで元の絶対時刻)で選ぶ。
            # - 枚数(-frames:v)で切ると、ソフトテレシネ(1枚が1.5フレーム分
            #   表示される)区間で枚数と時間が一致せず隣の区間にはみ出す
            #   (CS-Aの実録画で確認、2026-10-04)。
            # - ffmpegの-ss/-t/-toの終端判定はエンコーダーの時間単位への丸めに
            #   左右され、半フレーム位置のフレームで1枚ずれる(実測)。
            # そのためselectで「開始 <= t < 終了」を明示し、-toは読み込みを
            # 打ち切るためだけに余裕を持たせて使う。-fps_mode passthroughで
            # フレームの複製・間引きをさせない(ソフトテレシネの1.5フレーム
            # 間隔は1/2フレームに丸められるが、枚数と全体の時間は保たれる。
            # cfrはこの素材で数百フレームを捏造した、実測)。
            encoder = self._select_video_encoder(video_codec)
            field_filter = self._field_filter(fps)
            if field_filter:
                # repeatfieldsで作り直したフレームは時刻がフレームの刻みに揃って
                # いるので、刻みの境界(半フレーム手前)で選ぶ。
                start_abs = self._abs_time(part.start_frame - 0.5, fps)
                end_abs = self._abs_time(part.end_frame - 0.5, fps)
            select = f"select='gte(t,{start_abs:.6f})*lt(t,{end_abs:.6f})'"
            tail = [
                "-to", f"{end_abs + 1.0:.6f}", "-map", "0:v:0",
                "-vf", ",".join(f for f in (field_filter, select, hw_filter(encoder)) if f),
                "-fps_mode", "passthrough",
                "-c:v", encoder,
            ]
            if field_filter and encoder == "libx264":
                # ffmpeg 7.x の灰色の絵を避ける(compat.py)
                tail += x264_rate_args(fps)
            if self._output_interlaced():
                # 元がインターレースなら再エンコード部分もインターレース(フィールド)
                # 符号化にする。フィールド順は入力フレームの情報が引き継がれる。
                tail += ["-flags", "+ilme+ildct"]
            if smart_cut:
                # Smart CutのHead/TailはBフレームなし(符号化順=表示順)にする。
                # 後ろのコピー部分とのつなぎ目でデコード時刻を前倒しできるように
                # するため(_fit_decode_times)。1秒未満の部分なので画質への影響は小さい。
                tail += ["-bf", "0"]
            tail += self._rate_args(encoder, file_info)
            # VAAPIはデバイス指定を入力より前に置く必要がある。
            args = [
                "-y", *hw_input_args(encoder, self._encoder_devices.get(encoder)),
                *pre, "-i", str(file_info.file_path), *tail,
            ]

        args.append(str(out_path))
        self.ffmpeg.run(args, progress_callback=self._progress(
            part.phase, index, total, progress_callback, [(part.start_frame, part.end_frame)],
        ), bytes_per_frame=_bytes_per_frame(file_info))

    def _rate_args(self, encoder: str, file_info: FileCutInfo) -> List[str]:
        """画質は元ソースに合わせる(利用者が選ぶ設定はない)。"""
        args: List[str] = []
        if file_info.bitrate > 0:
            # 元ソースのビットレートに合わせる。
            # -b:v/-maxrate/-bufsizeはGPUエンコーダー含む全エンコーダー共通の
            # 汎用オプション(AVCodecContextレベル)。maxrate=1.5倍/bufsize=2倍は
            # 短区間エンコードでの瞬間的なビットレート跳ね上がりを抑える
            # 標準的なVBV設定。
            target = file_info.bitrate
            args += [
                "-b:v", str(target),
                "-maxrate", str(int(target * 1.5)),
                "-bufsize", str(target * 2),
            ]
        else:
            # ビットレートが分からない場合だけ固定品質。GPUにも各自の指標で渡す。
            args += quality_args(encoder, _FALLBACK_QUALITY)
        if encoder == "libx264":
            args += ["-preset", _X264_PRESET]
        if self.settings.cpu_threads:
            args += ["-threads", str(self.settings.cpu_threads)]
        return args

    # ---- MPEG-2のフィールド表示構造を保つ再エンコード -------------------------

    def _preserve_mpeg2_fields(self, video_codec: str) -> bool:
        return video_codec == "mpeg2video" and self._source_interlaced()

    def _render_mpeg2_preserving_fields(
        self,
        file_info: FileCutInfo,
        part: SegmentPart,
        out_path: Path,
        pre: List[str],
        start_pts: float,
        end_pts: float,
        index: int,
        total: int,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        """元の各コマの走査情報(プログレッシブ/インターレース、トップフィールド
        が先か、フィールドを繰り返すか)をそのまま保ってMPEG-2で再エンコードする。

        ソフトテレシネ(24コマのプログレッシブ+フィールド繰り返し指示)と
        インターレースが混在する録画(CS-A等)で、再エンコード部分も元と同じ
        表示構造・フレーム数にするため(ffmpegのMPEG-2エンコーダーは繰り返し
        指示を書き出せない)。手順:
        1. 区間の元のフレームの走査情報を読む
        2. プログレッシブが続く範囲/インターレースが続く範囲ごとに、それぞれの
           方式でBフレームなし(符号化順=表示順)に符号化する
        3. 各ピクチャのフラグを元の値へ書き戻し(mpeg2_flags)、つないでTSにする
        """
        fps = file_info.fps
        rate = Fraction(fps).limit_denominator(1001)
        source = self.ffmpeg.get_video_frames(
            file_info.file_path, self._abs_time(coarse_seek_frame(part.start_frame, self._keyframes, fps), fps) - 0.5,
            end_pts + 0.5,
        )
        frames = [f for f in source if start_pts - 0.001 <= f.pts < end_pts - 0.001]
        if not frames or abs(frames[0].pts - start_pts) > 0.001:
            raise MuxFailedError("E101", key="exceptions.reencode_no_frames", pts=f"{start_pts:.3f}")

        runs: List[List] = []
        for frame in frames:
            if runs and runs[-1][0].progressive == frame.progressive:
                runs[-1].append(frame)
            else:
                runs.append([frame])

        # GPU(Intel QSVのmpeg2_qsv)が使えればGPUで符号化する。1コマ1ピクチャ・
        # Bフレームなし・プログレッシブはフレームDCTと、書き戻しに必要な形で
        # 出力されることをCS-Aの実録画で確認した(2026-10-06)。万一合わない出力
        # だった時は、その範囲をソフトウェアでやり直し、このファイルの残りもソフトウェアにする。
        encoder = "mpeg2video" if self._mpeg2_gpu_failed else self._select_video_encoder("mpeg2video")
        flags_rate = frame_rate_code(fps)
        elementary = bytearray()
        for run_number, run in enumerate(runs):
            run_start = run[0].pts
            run_end = runs[run_number + 1][0].pts if run_number + 1 < len(runs) else end_pts
            # 範囲ごとに、その範囲の手前のキーフレームからデコードする。区間の
            # 先頭(pre)からだと、範囲の数だけ区間の頭から読み直すことになり、
            # 長い区間(Full Re-encode)では範囲の数の2乗に比例して遅くなった
            # (CS-Aの実録画で25分かけて約9%、2026-10-06)。
            if run_number:
                video_start = self._timing.video_start if self._timing else 0.0
                pre = self._coarse_seek_args(int((run_start - video_start) * fps), fps)
            flags = [PictureFlags(f.progressive, f.top_field_first, f.repeat_first_field) for f in run]
            video_start = self._timing.video_start if self._timing else 0.0
            run_frames = [(round((run_start - video_start) * fps), round((run_end - video_start) * fps))]
            try:
                elementary += self._encode_mpeg2_run(
                    encoder, file_info, pre, run_start, run_end, flags, flags_rate, rate,
                    self._progress(part.phase, index, total, progress_callback, run_frames, len(run)),
                )
            except MuxFailedError as e:
                if encoder == "mpeg2video":
                    raise
                logger.warning("The output of %s is unusable, redoing it in software: %s", encoder, e.log_message[-300:])
                encoder = "mpeg2video"
                self._mpeg2_gpu_failed = True
                elementary += self._encode_mpeg2_run(
                    encoder, file_info, pre, run_start, run_end, flags, flags_rate, rate,
                    self._progress(part.phase, index, total, progress_callback, run_frames, len(run)),
                )

        es_path = self.temp_manager.create_temp_file(suffix=".m2v")
        es_path.write_bytes(bytes(elementary))
        # 生のMPEG-2を読み込むと、時刻はフラグ(2/3フィールド)から計算される。
        # ただしffmpegはデータ末尾の1枚だけ1フィールド遅い時刻を付ける(実測)
        # ため、最後の1枚は元の時刻から求めた値で上書きする。
        # 1枚だけなら最後=最初で直す必要がない(しかも1枚にsettsを使うと
        # デコード時刻が失われ、連結後に時刻が約1.4秒戻った。BS-Aの実録画で確認)。
        mux = ["-y", "-fflags", "+genpts", "-f", "mpegvideo", "-i", str(es_path), "-c", "copy"]
        if len(frames) > 1:
            field = 1.0 / (2 * fps)
            last_offset = sum(3 if f.repeat_first_field else 2 for f in frames[:-1]) * field
            n = len(frames) - 1
            mux += ["-bsf:v", f"setts=pts='if(eq(N,{n}),STARTPTS+{last_offset:.9f}/TB,PTS)'"
                              f":dts='if(eq(N,{n}),STARTPTS+{last_offset:.9f}/TB,DTS)'"]
        self.ffmpeg.run([*mux, "-f", "mpegts", str(out_path)])

    def _encode_mpeg2_run(
        self,
        encoder: str,
        file_info: FileCutInfo,
        pre: List[str],
        run_start: float,
        run_end: float,
        flags: List[PictureFlags],
        flags_rate: int,
        rate: Fraction,
        progress: Optional[Callable[[int], None]],
    ) -> bytes:
        """走査方式が同じ1範囲を、Bフレームなし(符号化順=表示順)で符号化し、
        各ピクチャのフラグを元の値へ書き戻したMPEG-2のデータを返す。"""
        es_path = self.temp_manager.create_temp_file(suffix=".m2v")
        # 出力の時刻はフラグから計算し直すので、ここではfpsの等間隔に付け直す
        # (エンコーダーのフレームレート設定と時刻の丸め衝突を避けるため)。
        # 読み込みはtrimで打ち切る。-to(出力側)はsetptsで付け直した後の
        # 時刻(0から)と比べるので効かず、ffmpegがファイルの最後まで読み続け、
        # 範囲1つに約15秒かかっていた(CS-Aの実録画、2026-10-06)。
        filters = [
            f"trim=end={run_end + 1.0:.6f}",
            f"select='gte(t,{run_start - 0.001:.6f})*lt(t,{run_end - 0.001:.6f})'",
            f"setpts=N/({rate.numerator}/{rate.denominator})/TB,fps={rate.numerator}/{rate.denominator}",
            hw_filter(encoder),
        ]
        args = [
            "-y", *hw_input_args(encoder, self._encoder_devices.get(encoder)),
            *pre, "-i", str(file_info.file_path),
            "-to", f"{run_end + 1.0:.6f}", "-map", "0:v:0", "-vf", ",".join(f for f in filters if f),
            "-c:v", encoder, "-bf", "0",
        ]
        if not flags[0].progressive_frame:
            args += ["-flags", "+ilme+ildct"]
        args += self._rate_args(encoder, file_info)
        args += ["-f", "mpeg2video", str(es_path)]
        self.ffmpeg.run(args, progress_callback=progress)

        data = es_path.read_bytes()
        if count_pictures(data) != len(flags):
            raise MuxFailedError("E101", key="exceptions.picture_count_mismatch", got=count_pictures(data),
                                 expected=len(flags))
        try:
            return rewrite_field_flags(data, flags, flags_rate)
        except ValueError as e:
            raise MuxFailedError("E101", key="exceptions.field_rewrite_failed", error=e)

    # ---- つなぎ目の H.264 の絵の順番 ----------------------------------------------------

    def _continue_copied_order(
        self, file_info: FileCutInfo, parts: List[SegmentPart], video_files: List[Tuple[Path, float]]
    ) -> None:
        """H.264 のコピーの部分の POC を、前の部分(Head やコピー)に続くよう書き換える(native/src/h264.rs)。

        放送の H.264 はキーフレームが IDR でない I のことが多く、前の部分の後ろにつなぐと、I の下位の値に
        よっては I の POC が前の部分より前になり、ffmpeg のデコーダーが後ろの B フレームを約 20 秒にわたって
        捨てた(Smart Cut で約 150 枚、Simple Copy で 89 枚。テスト動画 2 本、2026-11-17)。書き換えられない時
        (ts_smartcut_native が無い等)は、警告を残してそのままつなぐ。"""
        if file_info.video_codec != "h264" or not any(p.action == "copy" for p in parts[1:]):
            return
        from ts_smartcut.autocut.native import NativeModuleError, native_module

        try:
            native = native_module()
        except NativeModuleError as e:
            logger.warning("Cannot continue the H.264 picture order at joins, B-frames after them may not play: %s",
                           e.log_message)
            return
        for index in range(1, len(parts)):
            if parts[index].action != "copy":
                continue
            previous, path = video_files[index - 1][0], video_files[index][0]
            try:
                tail = native.h264_tail_order(str(previous))
                if tail is None:
                    logger.warning("The part before %s has no picture order to continue", path.name)
                    continue
                _, last_lsb, ahead = tail
                added = native.h264_continue_order(str(path), last_lsb, ahead)
            except ValueError as e:
                logger.warning("Could not continue the H.264 picture order into %s: %s", path.name, e)
                continue
            if added is not None:
                logger.info("Continued the H.264 picture order into %s (+%d)", path.name, added)

    # ---- つなぎ目のデコード時刻 -------------------------------------------------

    def _fit_decode_times(
        self, file_info: FileCutInfo, parts: List[SegmentPart], video_files: List[Tuple[Path, float]]
    ) -> None:
        """再エンコード部分のデコード時刻を、前後の部分とつながるように調整する。

        コピー部分の先頭(Iフレーム)は、元のBフレームの並べ替えのため表示より
        2〜6フィールド早くデコードされる必要がある。一方、Bフレームなしで
        再エンコードした部分はデコードが表示の約1フレーム前と遅めなので、
        そのまま連結すると「次の部分の先頭が、この部分の最後より先にデコード
        される」逆転が起き、ffmpegが1刻み(1/90000秒)ずらして辻褄を合わせて
        いた(実録画のつなぎ目で確認、2026-10-04)。

        Bフレームなしの部分はデコード時刻を表示時刻以下で自由に決められる。
        後ろの部分から順に、次の部分の先頭より1フレーム以上前に最後のフレーム
        をデコードできるだけ前倒しする。前の部分も再エンコード部分なら、必要な
        前倒しをその部分へ受け渡す(カットをまたぐ短いHeadでは自分だけでは
        余地が足りなかった)。動かせないのはコピー部分だけ。
        各部分の中では、最初→最後へ直線的にずれ幅を変える。"""
        frame = 1.0 / file_info.fps
        count = len(parts)
        # 各部分のデコード時刻情報(部分の先頭=0とした相対値)
        info: List[Optional[dict]] = [None] * count

        def load(i: int) -> Optional[dict]:
            if info[i] is not None:
                return info[i]
            path, duration = video_files[i]
            if parts[i].action == "reencode" or duration <= _TAIL_READ_SECONDS:
                # 再エンコード部分と短いコピー部分は全体を読む(短い部分はシークしても
                # 先頭から読むのと同じ量なので、シークしない)
                start, packets = self.ffmpeg.get_video_decode_order(path)
                if parts[i].action != "reencode" and packets:
                    packets = packets[:1] + packets[-1:]
            else:
                start, head = self.ffmpeg.get_video_decode_order(path, read_intervals="%+#5")
                tail = self._read_tail_packets(path, duration)
                packets = head[:1] + tail[-1:] if head and tail else []
            if start is None or not packets or any(d is None for _, d in packets):
                # 前後の部分のデコード時刻が分からないと、そのつなぎ目は調整できない
                # (ffmpeg が 1/90000 秒ずらして辻褄を合わせる)。出力は使えるので続ける
                logger.warning("Could not read the decode times used to fit a join: %s", path.name)
                return None
            pts_order = [p for p, _ in packets]
            info[i] = {
                # デコード順のまま表示時刻が増え続ける(=並べ替えのない)部分だけ
                # デコード時刻を自由に決められる
                "adjustable": parts[i].action == "reencode"
                and all(b > a for a, b in zip(pts_order, pts_order[1:])),
                "pts": [p - start for p, _ in packets],
                "lags": [p - d for p, d in packets],
                "first_dts": packets[0][1] - start,
                "last_dts": packets[-1][1] - start,
                "duration": duration,
            }
            return info[i]

        # 再エンコード部分の新しいずれ幅(最初, 最後)と、それによる先頭のデコード時刻
        plan: Dict[int, Tuple[float, float]] = {}
        first_dts: Dict[int, float] = {}
        for i in range(count - 1, -1, -1):
            if parts[i].action != "reencode":
                continue
            own = load(i)
            if own is None or not own["adjustable"]:
                continue
            pts, lags = own["pts"], own["lags"]
            lag_start, lag_end = lags[0], lags[-1]

            need_end = lag_end
            if i + 1 < count:
                nxt = load(i + 1)
                if nxt is not None:
                    next_first = first_dts.get(i + 1, nxt["first_dts"])
                    need_end = max(lag_end, pts[-1] - (own["duration"] + next_first) + frame)

            # 前が動かせない部分(コピー等)なら、その最後のデコード時刻より後で
            # なければならない。前も調整できる部分なら、そちらを前倒しして場所を空ける。
            max_start = math.inf
            prev = load(i - 1) if i > 0 else None
            if prev is not None and not prev["adjustable"]:
                prev_last = prev["last_dts"] - prev["duration"]
                max_start = pts[0] - prev_last - frame

            steps = len(pts) - 1
            min_step = min((b - a for a, b in zip(pts, pts[1:])), default=frame)
            start_needed = need_end
            if steps and max_start == math.inf:
                # 前も調整できる部分なら、前倒しの一部は自分の中で受け持つ(1段ごとにデコード時刻の
                # 間隔が1フィールド以上残る分まで)。全部を先頭のずれにすると前の短いTailに押し付け、
                # そこでデコード時刻が1フィールド未満に詰まった(フィールドで書かれたH.264のBodyは
                # 先頭のデコードが5フィールド早い。実録画、2026-11-17)
                start_needed = need_end - steps * max(0.0, min_step - frame / 2) * 0.9
            new_start = max(0.0, min(max(start_needed, lag_start), max_start))
            if lag_start - frame * 0.1 <= new_start < lag_start:
                # 前のcopy部分との1フレームの間隔にわずかに足りないだけなら元のまま
                # にして末尾側の前倒しは行う(0.1ms足りないだけで調整を丸ごと諦め、
                # 次のつなぎ目が詰まっていた。CS-Aの実録画、2026-10-05)。
                new_start = lag_start
            if new_start < lag_start - 1e-6:
                logger.warning(
                    "Cannot fit the decode times at a join: %s (needed lead %.4fs, limit %.4fs, original %.4fs)",
                    video_files[i][0].name, need_end, max_start, lag_start,
                )
                continue
            new_end = max(need_end, new_start)
            if steps:
                if (new_end - new_start) / steps >= min_step:
                    logger.warning("Cannot move the decode times at a join early enough: %s", video_files[i][0].name)
                    new_end = new_start + min_step * 0.9 * steps
            if abs(new_start - lag_start) < 1e-6 and abs(new_end - lag_end) < 1e-6:
                continue
            plan[i] = (new_start, new_end)
            first_dts[i] = pts[0] - new_start

        for i, (lag_first, lag_last) in plan.items():
            path, duration = video_files[i]
            steps = max(len(info[i]["pts"]) - 1, 1)
            slope = (lag_last - lag_first) / steps
            fitted = path.with_name(path.stem + "_dts" + path.suffix)
            self.ffmpeg.run([
                "-y", "-copyts", "-i", str(path), "-map", "0:v:0", "-c", "copy",
                # ptsも明示する(dtsだけ指定するとptsがdtsの値で上書きされた、実測)
                "-bsf:v", f"setts=pts='PTS':dts='PTS-({lag_first:.9f}+{slope:.9f}*N)/TB'",
                "-f", "mpegts", str(fitted),
            ])
            self.temp_manager.active_files.append(fitted)
            video_files[i] = (fitted, duration)

    def _tail_interval(self, path: Path, duration: float) -> str:
        """部分ファイルの末尾付近だけを読むためのread_intervals。"""
        start, _ = self.ffmpeg.get_video_decode_order(path, read_intervals="%+#1")
        return f"{(start or 0.0) + max(0.0, duration - _TAIL_READ_SECONDS):.3f}%"

    def _read_tail_packets(self, path: Path, duration: float) -> List[Tuple[float, Optional[float]]]:
        """部分ファイルの末尾付近のパケット。TS のシーク読みは、まれにエラーなしで
        何も返さなかった(テスト用の映像で約 300 回に 1 回、同じファイルを読み直すと
        返る。2026-10-09)。空なら 1 回だけ読み直し、それでも空ならファイル全体を読む
        (失敗した時だけなので、ふだんの処理時間は変わらない)。"""
        interval = self._tail_interval(path, duration)
        for _ in range(2):
            _, tail = self.ffmpeg.get_video_decode_order(path, read_intervals=interval)
            if tail:
                return tail
        logger.info("Could not read the end of a part file, reading all of it: %s", path.name)
        _, packets = self.ffmpeg.get_video_decode_order(path)
        return packets

    # ---- 音声 ------------------------------------------------------------

    def _render_audio_runs(
        self,
        file_info: FileCutInfo,
        audio_runs: List[Tuple[int, int]],
        suffix: str,
        audio_codec: str,
        first_step: int,
        total_steps: int,
        progress_callback: Optional[ProgressCallback],
    ) -> List[Tuple[Path, float]]:
        """残す連続区間ごとに音声を切り出す。音声はMatroska(読めない時はNUT)で書き出す。
        TSは多重化遅延(約1.4秒)を足すうえ、区間の切り出し位置と最初の音声パケットの
        微小なずれを保持できない。"""
        audio_files = []
        for run_index, (run_start, run_end) in enumerate(audio_runs):
            audio_path = self.temp_manager.create_temp_file(suffix=suffix)
            self._render_audio(
                file_info, run_start, run_end, audio_path, audio_codec,
                first_step + run_index, total_steps, progress_callback,
            )
            audio_files.append((audio_path, self._span(file_info, run_start, run_end)))
        return audio_files

    def _render_audio(
        self,
        file_info: FileCutInfo,
        start_frame: int,
        end_frame: int,
        out_path: Path,
        audio_codec: str,
        index: int,
        total: int,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        """連続区間[start_frame, end_frame)の音声を映像と同じ時刻で切り出す。
        copyではパケット単位でしか切れないため、開始は区間内の最初の
        パケット、終了は区間からはみ出さない最後のパケットまでとする
        （次の区間と重ならないようにするため。つなぎ目に最大2パケット分
        (AC-3で約64ms)の無音ができるが、ずれは蓄積しない）。"""
        fps = file_info.fps
        duration = self._span(file_info, start_frame, end_frame)
        pre = self._coarse_seek_args(start_frame, fps)
        post = ["-ss", f"{self._boundary(file_info, start_frame):.6f}"]
        if audio_codec == "copy":
            keep = max(duration - self._timing.audio_packet_duration + 0.001, 0.001)
        else:
            keep = duration  # 再エンコードならサンプル単位で正確に切れる
        probe, audio_map = audio_input(file_info)
        args = [
            # 受信エラー(ドロップ)で壊れた音声パケットは捨てる。AACをMatroskaへ書く時に
            # ffmpegが自動で通すaac_adtstoascが壊れたADTSヘッダーで止まり、録画全体が
            # E101になっていた(CS-Bの_drop録画、2026-10-10)。捨てるのは
            # 分離器が壊れていると印を付けたパケットだけ(実録画で84703中7個)
            "-y", "-fflags", "+discardcorrupt", *probe, *pre, "-i", str(file_info.file_path), *post,
            "-map", audio_map, "-t", f"{keep:.6f}", "-c:a", audio_codec,
            str(out_path),
        ]
        # 音声だけの出力なのでffmpegはフレーム数を出さない。段階の始まりだけ知らせる。
        self._progress("Audio", index, total, progress_callback)
        self.ffmpeg.run(args)

    # ---- 連結 ------------------------------------------------------------

    def _join(
        self,
        file_info: FileCutInfo,
        video_files: List[Tuple[Path, float]],
        audio_files: List[Tuple[Path, float]],
        output_path: Path,
        total: int,
        progress_callback: Optional[ProgressCallback],
        spans: Optional[List[Tuple[int, int]]] = None,
    ) -> None:
        """映像ピースと音声区間をそれぞれconcatデムクサで連結し、1つにまとめる。
        spansは各映像ピースの元のフレーム範囲(進捗の表示用)。
        各ファイルの長さは元のフレームの表示時刻から求めた正確な値をdurationで
        指定する（ファイルの実測長は音声パケットの端数等で揺れ、つなぎ目が
        ずれるため）。"""

        def _write_list(entries: List[Tuple[Path, float]], from_zero: bool) -> Path:
            list_path = self.temp_manager.create_temp_file(suffix=".txt")
            lines = []
            for path, seconds in entries:
                # '…' の中で特別なのは ' だけ。'\'' で閉じて書き、また開く。出力先の
                # フォルダ名に ' があると結合が必ず失敗していた(2026-10-22)
                quoted = path.as_posix().replace("'", "'\\''")
                lines.append(f"file '{quoted}'")
                if from_zero:
                    # 音声ファイルの時刻0=区間の開始位置(出力側-ssで切り出した点。
                    # copyでは-copyts下でも時刻はこの点基準になる)。concatは既定で
                    # 各ファイルの最初のパケットを区間の先頭とみなすため、切り出し
                    # 位置と最初のパケットのずれ(最大1パケット)が失われる。
                    lines.append("inpoint 0")
                lines.append(f"duration {seconds:.6f}")
            list_path.write_text("\n".join(lines), encoding="utf-8")
            return list_path

        # -copyts: 映像(0秒始まり)と音声(最初のパケットの位置から始まる)の
        # 相対関係を保つ。既定では入力ごとに開始時刻を0へ揃えてしまう。
        args = ["-y", "-copyts", "-f", "concat", "-safe", "0", "-i", str(_write_list(video_files, False))]
        if audio_files:
            args += ["-f", "concat", "-safe", "0", "-i", str(_write_list(audio_files, True))]
            args += ["-map", "0:v:0", "-map", "1:a:0"]
        args += ["-c", "copy"]
        if output_path.suffix.lower() in _MPEGTS_SUFFIXES:
            # TSは既定で複数の音声フレームを1つのPESにまとめ、先頭にしか時刻を
            # 持たない。つなぎ目の小さな隙間がまとめられたフレームに吸収されて
            # 時刻がずれるため、音声フレームごとにPESを分ける。
            args += ["-pes_payload_size", "0"]
        args.append(str(output_path))
        self.ffmpeg.run(args, progress_callback=self._progress("Concat", total - 1, total, progress_callback, spans),
                        bytes_per_frame=_bytes_per_frame(file_info))

    @staticmethod
    def _progress(
        phase: str,
        index: int,
        total: int,
        progress_callback: Optional[ProgressCallback],
        spans: Optional[List[Tuple[int, int]]] = None,
        expected_frames: Optional[int] = None,
    ) -> Optional[Callable[[int], None]]:
        """段階の始まりを知らせ、ffmpegが出す出力済みフレーム数を進捗へ変える
        関数を返す。spansはこの段階が出力する元のフレーム範囲(順に連結される)、
        expected_framesは出力されるフレーム数(省略時はspansの合計)。ソフト
        テレシネのコピーでは実際の枚数がこれより少なく、進捗は段階の終わりで
        飛ぶことがある(表示用なので許容)。"""
        if progress_callback is None:
            return None
        spans = spans or []
        span_total = sum(max(e - s, 0) for s, e in spans)
        expected = expected_frames or span_total

        def _source_frame(fraction: float) -> Optional[int]:
            if not spans:
                return None
            remaining = fraction * span_total
            for start, end in spans:
                length = max(end - start, 0)
                if remaining < length:
                    return start + int(remaining)
                remaining -= length
            return max(spans[-1][1] - 1, spans[-1][0])

        def _on_progress(frames_done: int) -> None:
            fraction = min(frames_done / expected, 1.0) if expected > 0 else 0.0
            progress_callback(ProcessingProgressEvent(phase, index, total, fraction, _source_frame(fraction)))

        progress_callback(ProcessingProgressEvent(phase, index, total, 0.0, _source_frame(0.0)))
        return _on_progress
