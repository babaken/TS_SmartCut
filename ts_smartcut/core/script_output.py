"""出力形式 AVS／VPY: カット情報を書いた AviSynth／VapourSynth のスクリプト (設計書 §6.9)。

スクリプトと一緒に、タイムコード(<名前>.timecode.txt)と残す区間の音声(<名前>.flac)を書く。動画は
変換しない。読み込みの式と区間はアプリが入れ、利用者のテンプレートの決まった文字を置き換える。

- 動画は元の録画を L-SMASH Works の repeat=0 で読む。フレーム k は「録画の中で表示の順に k 番目の絵」
  (デコードできない先頭の絵も数える。2026-10-31 の試作で、Windows 8 本・Linux 4 本で確かめた)。
  スクリプトはその絵をフィールドに分け、アプリのフレーム N の時刻(video_start + N/fps)に表示されるフィールドを
  選んでフレームに戻す。出力は元の録画と同じ 29.97i(固定フレームレート)で、フレーム N はアプリのフレーム N。
  ソフトテレシネの所は、デコーダーが印どおりにフィールドを繰り返したのと同じになる(2026-11-02、利用者の決定。
  絵を 1 枚 1 フレームのまま出すと、クリップは可変フレームレートなのに L-SMASH Works が録画全体の平均の fps を
  付けるので、再生の時間がカット後の時間とずれた)。
- 音声はアプリが残す区間だけを FLAC(16 ビット)に書き、スクリプトはそれを L-SMASH Works で読む
  (2026-11-01、利用者の決定)。元の録画の音声をスクリプトで切ると、L-SMASH Works は途中へシークした時に
  位置によって 0〜5120 サンプルずれ、BestSource は受信エラーの所で音声が終わった。どちらも受信エラーで
  音声が抜けた所を詰めるので、その後ろで音が映像より早くなる。FLAC は、音声を時刻どおりに並べ(抜けは
  無音で埋める)、各区間を映像と同じ時刻・同じ長さで切ってつなぐ。
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ts_smartcut.errors import FFprobeError, FileIOError, ValidationError
from ts_smartcut.models import CutRange, FileCutInfo

OUTPUT_TS = "ts"
OUTPUT_AVS = "avs"
OUTPUT_VPY = "vpy"
OUTPUT_FORMATS = (OUTPUT_TS, OUTPUT_AVS, OUTPUT_VPY)
SCRIPT_FORMATS = (OUTPUT_AVS, OUTPUT_VPY)
SCRIPT_EXTENSIONS = {OUTPUT_AVS: ".avs", OUTPUT_VPY: ".vpy"}
# スクリプトと一緒に書くファイル: <スクリプトの名前から拡張子を除いたもの><後ろに付ける文字>。
# 名前の連番はスクリプトとそろえる
TIMECODE_SUFFIX = ".timecode.txt"  # timecode format v2
AUDIO_SUFFIX = ".flac"
COMPANION_SUFFIXES = (TIMECODE_SUFFIX, AUDIO_SUFFIX)

PLACEHOLDER_VIDEO = "__TSSC_VIDEO__"
PLACEHOLDER_AUDIO = "__TSSC_AUDIO__"
PLACEHOLDER_TRIM = "__TSSC_TRIM__"
PLACEHOLDERS = (PLACEHOLDER_VIDEO, PLACEHOLDER_AUDIO, PLACEHOLDER_TRIM)
_PLACEHOLDER_RE = re.compile(r"__TSSC_[A-Za-z0-9_]*?__")
# FLAC に渡す音声のかたまりの大きさ(サンプル。ffmpeg の flac の既定のブロックの大きさ)
FLAC_BLOCK_SAMPLES = 4608
# フィールドの並びの繰り返しの長さ: 2(インターレース・ソフトテレシネでない所)と 10(3:2、絵 4 枚で 8 フィールド)
FIELD_PERIODS = (2, 10)
# 時刻の比べの余裕(フレームの何分のいくつか)。絵の表示時刻は 90 kHz に丸めてあり、フィールドの長さ(1501.5)は割り切れない
_FRAME_TOLERANCE = 0.125
# AVS の 1 行に書くかたまりの数(長い 1 行を避ける)
_PIECES_PER_LINE = 20

DEFAULT_TEMPLATES = {
    OUTPUT_AVS: (
        "v = __TSSC_VIDEO__\n"
        "a = __TSSC_AUDIO__\n"
        "__TSSC_TRIM__\n"
        "AudioDub(v, a)\n"
    ),
    OUTPUT_VPY: (
        "import vapoursynth as vs\n"
        "core = vs.core\n"
        "v = __TSSC_VIDEO__\n"
        "a = __TSSC_AUDIO__\n"
        "__TSSC_TRIM__\n"
        "v.set_output(0)\n"
        "a.set_output(1)\n"
    ),
}


@dataclass(frozen=True)
class ScriptPlan:
    """1 本のスクリプトの中身: 残すフレーム [start, end) の一覧、出力の各フレームのフィールド(field_numbers)と、
    各区間の音声(録画の時刻 t、サンプル数)。"""

    ranges: List[Tuple[int, int]]
    fields: List[int]
    fps: float
    top_field_first: bool
    audio_segments: List[Tuple[float, int]]
    sample_rate: int
    format_start: float  # 録画の start_time(音声のサンプルを数える基準)
    audio_probe_args: List[str]
    audio_map: str  # ffmpeg の音声の指定(0:a:0 か 0:i:<PID>)

    @property
    def frame_count(self) -> int:
        return sum(n2 - n1 for n1, n2 in self.ranges)


@dataclass(frozen=True)
class FieldPiece:
    """フィールドの番号の並びのひとかたまり: 録画のフィールド base から、cycle 枚ごとに offsets の順で選んだ length 枚。"""

    base: int
    cycle: int
    offsets: Tuple[int, ...]
    length: int

    @property
    def contiguous(self) -> bool:
        return self.offsets == tuple(range(self.cycle))


def is_script_format(output_format: str) -> bool:
    return output_format in SCRIPT_FORMATS


def companion_path(script_path: Path, suffix: str) -> Path:
    return script_path.with_name(script_path.stem + suffix)


def frame_ranges(cuts: Sequence[CutRange], video_start: float, fps: float, end_time: float,
                 first_decodable_pts: Optional[float] = None) -> List[Tuple[int, int]]:
    """残す区間(アプリのフレーム [N1, N2))を、出力できるフレームに狭める。デコードできない先頭の絵が出るフレームは
    TS の出力と同じく出さない。録画の終わり(最後の絵の終わり end_time)より後ろのフレームも出さない。空になった
    区間は除き、接する・重なる区間は 1 つにする。"""
    first = 0
    if first_decodable_pts is not None:
        first = max(0, math.ceil((first_decodable_pts - video_start) * fps - _FRAME_TOLERANCE))
    last = math.floor((end_time - video_start) * fps + _FRAME_TOLERANCE)
    ranges: List[Tuple[int, int]] = []
    for cut in sorted(cuts, key=lambda c: c.start_frame):
        n1, n2 = max(cut.start_frame, first), min(cut.end_frame, last)
        if n2 <= n1:
            continue
        if ranges and n1 <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], n2))
        else:
            ranges.append((n1, n2))
    return ranges


def field_numbers(ranges: Sequence[Tuple[int, int]], times: Sequence[float], video_start: float, fps: float,
                  top_field_first: bool) -> List[int]:
    """出力の各フレームの 2 つのフィールドを、録画の絵のフィールドの番号で表示の順に並べる(SeparateFields の番号:
    絵 p の上のフィールドが 2p、下が 2p+1。times は絵の表示時刻を表示の順に並べたもの)。

    アプリのフレーム N は時刻 video_start + N/fps からの 2 フィールド。それぞれのフィールドの時刻に表示されている
    絵(表示時刻がその時刻以前の最後の絵)の、そのフィールドの側(上か下)を使う。ソフトテレシネの絵は 2 か 3
    フィールド表示されるので、デコーダーが印どおりにフィールドを繰り返したのと同じ 29.97i になる(2026-11-02 の
    試作で、ffmpeg の repeatfields とすべてのフレームがビット単位で同じだった)。受信エラーで絵が抜けた所は前の絵が
    続く。top_field_first なら、各フレームの先のフィールドが上。"""
    result: List[int] = []
    for n1, n2 in ranges:
        for n in range(n1, n2):
            for half, top in ((0, top_field_first), (1, not top_field_first)):
                t = video_start + (n + 0.5 * half + _FRAME_TOLERANCE) / fps
                p = max(bisect.bisect_right(times, t) - 1, 0)
                result.append(2 * p + (0 if top else 1))
    return result


def field_pieces(fields: Sequence[int]) -> List[FieldPiece]:
    """フィールドの番号の並びを、同じ形の繰り返しのかたまりにする(スクリプトに書く Trim・SelectEvery の数を減らす)。
    かたまりの始まりは、SelectEvery の offsets が cycle に収まる位置までずらし、その前は 1 枚ずつにする。実録画 15 本
    (30〜88 分)で、録画全体が 6〜789 かたまりだった。"""
    pieces: List[FieldPiece] = []
    total = len(fields)
    i = 0
    while i < total:
        length, period, step = 1, 1, 1
        for p in FIELD_PERIODS:
            if i + 2 * p > total:
                continue
            d = fields[i + p] - fields[i]
            if d <= 0:
                continue
            j = i + p
            while j < total and fields[j] - fields[j - p] == d:
                j += 1
            if j - i > length:
                length, period, step = j - i, p, d
        shift = 0
        if period > 1:
            while shift < period and (max(fields[i + shift:i + shift + period])
                                      - min(fields[i + shift:i + shift + period])) >= step:
                shift += 1
            if shift == period:
                shift = length  # 収まる位置が無い: 全部 1 枚ずつ
        for k in range(i, i + min(shift, length)):
            pieces.append(FieldPiece(fields[k], 1, (0,), 1))
        if shift < length:
            block = fields[i + shift:i + shift + period]
            base = min(block)
            pieces.append(FieldPiece(base, step, tuple(f - base for f in block), length - shift))
        i += length
    return pieces


def audio_segments(ranges: Sequence[Tuple[int, int]], video_start: float, fps: float,
                   sample_rate: int) -> List[Tuple[float, int]]:
    """各区間の音声: (区間の最初のフレームの時刻, サンプル数)。長さは映像と同じフレームの数の時間で、つないだ後の
    時刻で丸めるので、区間が多くても映像とずれない。"""
    result = []
    frames = 0
    for n1, n2 in ranges:
        samples = round((frames + n2 - n1) / fps * sample_rate) - round(frames / fps * sample_rate)
        frames += n2 - n1
        result.append((video_start + n1 / fps, samples))
    return result


def audio_filter(segments: Sequence[Tuple[float, int]], format_start: float, sample_rate: int,
                 audio_map: str) -> str:
    """残す区間の音声を 1 つにつなぐ ffmpeg の filter_complex(出力は [out])。音声を録画の start_time からの
    時刻どおりに並べ(受信エラーで抜けた所は無音で埋め、重なりは捨てる。-copyts で読む)、asegment で区間の
    境目のサンプルで分け、残す区間だけをつなぐ。録画の終わりより後ろの区間は無音で長さを合わせる。
    最後に音声のかたまりの大きさをそろえる(FLAC のエンコーダーは最初のかたまりの大きさをブロックの大きさにする。
    区間の境目で切った 13 サンプルのかたまりが最初に来て「invalid block size」で失敗した録画があった)。"""
    bounds: List[int] = []
    for start, samples in segments:
        first = max(round((start - format_start) * sample_rate), bounds[-1] if bounds else 0)
        bounds += [first, first + samples]
    total = sum(samples for _, samples in segments)
    outputs = "".join(f"[s{i}]" for i in range(len(bounds) + 1))
    kept = "".join(f"[s{2 * i + 1}]" for i in range(len(segments)))
    discarded = ";".join(f"[s{i}]anullsink" for i in range(0, len(bounds) + 1, 2))
    return (f"[{audio_map}]asetpts=PTS-{format_start!r}/TB,aresample=async=1:min_hard_comp=0.01:first_pts=0,"
            f"asegment=samples={'|'.join(map(str, bounds))}{outputs};{discarded};"
            f"{kept}concat=n={len(segments)}:v=0:a=1,apad=whole_len={total},atrim=end_sample={total},"
            f"asetnsamples=n={FLAC_BLOCK_SAMPLES}:p=0[out]")


def audio_args(plan: ScriptPlan, source: Path, output: Path) -> List[str]:
    """残す区間の音声を FLAC(16 ビット)に書く ffmpeg の引数。受信エラーの AAC は捨てる(TS の出力と同じ)。"""
    return [
        "-hide_banner", "-nostdin", "-y", "-copyts", "-fflags", "+discardcorrupt", *plan.audio_probe_args,
        "-i", str(source),
        "-filter_complex", audio_filter(plan.audio_segments, plan.format_start, plan.sample_rate, plan.audio_map),
        "-map", "[out]", "-c:a", "flac", "-sample_fmt", "s16", str(output),
    ]


def timecode_text(frame_count: int, fps: float) -> str:
    """出力の各フレームの開始時刻(ミリ秒、timecode format v2)。出力は固定フレームレートなので等間隔。"""
    return "# timecode format v2\n" + "".join(f"{n * 1000.0 / fps:.3f}\n" for n in range(frame_count))


def check_template(text: str) -> None:
    """置き換える文字がそろっているか。無い・知らない __TSSC_…__ があれば E205。"""
    for placeholder in PLACEHOLDERS:
        if placeholder not in text:
            raise ValidationError("E205", key="exceptions.template_missing", placeholder=placeholder)
    for found in _PLACEHOLDER_RE.findall(text):
        if found not in PLACEHOLDERS:
            raise ValidationError("E205", key="exceptions.template_unknown", placeholder=found)


def load_template(output_format: str, path: Optional[Path]) -> str:
    """テンプレートの文字(UTF-8)。path が無ければ同梱の既定。読めないファイルは E001。"""
    if path is None:
        return DEFAULT_TEMPLATES[output_format]
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise FileIOError("E001", key="exceptions.template_read_failed", path=path, error=e) from e


def _avs_string(value: str) -> str:
    # AviSynth の "…" の中では " を書けない(Windows のパスには無い)ので、その時は """…""" にする
    if '"' not in value:
        return f'"{value}"'
    if '"""' in value or value.endswith('"'):
        raise ValidationError("E205", key="exceptions.script_path_quote", path=value)
    return f'"""{value}"""'


def _vpy_string(value: str) -> str:
    return repr(value)


def source_expressions(output_format: str, source: Path, audio: Path, cache_dir: Path) -> Tuple[str, str]:
    """(動画の読み込みの式, 音声の読み込みの式)。動画は元の録画、音声はアプリが書いた FLAC を L-SMASH Works で
    読む。パスは絶対パス(スクリプトは録画と別のフォルダに置く)。索引(.lwi)は cache_dir に作らせる(録画の
    横には作らない)。"""
    src, flac, cache = (str(Path(p).resolve()) for p in (source, audio, cache_dir))
    if output_format == OUTPUT_AVS:
        c = _avs_string(cache)
        return (f"LWLibavVideoSource({_avs_string(src)}, repeat=false, cachedir={c})",
                f"LWLibavAudioSource({_avs_string(flac)}, cachedir={c})")
    c = _vpy_string(cache)
    return (f"core.lsmas.LWLibavSource({_vpy_string(src)}, repeat=0, cachedir={c})",
            f"core.lsmas.LWLibavAudioSource({_vpy_string(flac)}, cachedir={c})")


def fps_fraction(fps: float) -> Tuple[int, int]:
    """fps を分数にする(29.97 は 30000/1001)。"""
    for den in (1, 1001):
        num = round(fps * den)
        if abs(num / den - fps) < 1e-6:
            return num, den
    fraction = Fraction(fps).limit_denominator(1001)
    return fraction.numerator, fraction.denominator


def _avs_piece(piece: FieldPiece) -> str:
    if piece.contiguous:
        return f"Trim(v, {piece.base}, -{piece.length})"
    offsets = ", ".join(map(str, piece.offsets))
    # SelectEvery は fps を変え、AviSynth の ++ は fps が違うとつなげないので、フィールドの fps に戻す
    return f"SelectEvery(Trim(v, {piece.base}, 0), {piece.cycle}, {offsets}).AssumeFPS(v).Trim(0, -{piece.length})"


def _vpy_piece(piece: FieldPiece) -> str:
    if piece.contiguous:
        return f"v[{piece.base}:{piece.base + piece.length}]"
    return f"core.std.SelectEvery(v[{piece.base}:], {piece.cycle}, {list(piece.offsets)})[:{piece.length}]"


def trim_statements(output_format: str, plan: ScriptPlan) -> str:
    """動画 v(録画の絵を 1 枚 1 フレームで読んだもの)を、残す区間だけの固定フレームレート(29.97i など)にする文
    (音声 a はもう残す区間だけの FLAC なので切らない)。絵をフィールドに分け、出力の各フレームのフィールドを
    選んで(field_numbers)つなぎ、フレームに戻す。"""
    pieces = field_pieces(plan.fields)
    num, den = fps_fraction(plan.fps)
    if output_format == OUTPUT_AVS:
        lines = ["v = v.AssumeTFF().SeparateFields()"]
        for i in range(0, len(pieces), _PIECES_PER_LINE):
            chunk = " ++ ".join(_avs_piece(p) for p in pieces[i:i + _PIECES_PER_LINE])
            lines.append(f"tssc_fields = {chunk}" if i == 0 else f"tssc_fields = tssc_fields ++ {chunk}")
        order = "AssumeTFF" if plan.top_field_first else "AssumeBFF"
        # Weave の後も、最初の絵の _FieldBased が残る(プログレッシブの絵なら、ffmpeg などは全体をプログレッシブと
        # 読む)ので付け直す(2 = 上が先、1 = 下が先)
        field_based = 2 if plan.top_field_first else 1
        lines.append(f"v = tssc_fields.AssumeFieldBased().{order}().Weave().AssumeFPS({num}, {den})"
                     f'.propSet("_FieldBased", {field_based})')
        return "\n".join(lines)
    # VapourSynth の SeparateFields は、絵の _FieldBased があると tff より先にそれを使うので消す(番号を
    # 上 2p・下 2p+1 にそろえる)
    lines = ['v = core.std.SeparateFields(core.std.RemoveFrameProps(v, "_FieldBased"), tff=True)',
             "v = core.std.Splice(["]
    lines += [f"    {_vpy_piece(p)}," for p in pieces]
    lines += ["])",
              f"v = core.std.DoubleWeave(v, tff={plan.top_field_first})[::2]",
              f"v = core.std.AssumeFPS(v, fpsnum={num}, fpsden={den})"]
    return "\n".join(lines)


def render_script(output_format: str, template: str, video_expression: str, audio_expression: str,
                  plan: ScriptPlan) -> str:
    check_template(template)
    if not plan.ranges:
        raise ValidationError("E201", key="exceptions.no_keep_ranges")
    return (template.replace(PLACEHOLDER_VIDEO, video_expression)
            .replace(PLACEHOLDER_AUDIO, audio_expression)
            .replace(PLACEHOLDER_TRIM, trim_statements(output_format, plan)))


def plan_script(ffmpeg, file_info: FileCutInfo) -> ScriptPlan:
    """録画 file_info の残す区間を、出力のフレームのフィールドと音声の区間にする。録画全体を 1 回読む
    (映像のパケット)。"""
    from ts_smartcut.ffmpeg.smart_cut import audio_input

    if not file_info.cuts:
        raise ValidationError("E201", key="exceptions.no_keep_ranges")
    source = file_info.file_path
    # 選んだ音声(設計書 §4.12)。最初の音声以外は、番組の途中の PMT で加わることがあるので広く読む
    probe_args, audio_map = audio_input(file_info)
    data = ffmpeg.probe(source, probe_args)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFprobeError("E102", key="exceptions.no_video_stream", path=source)
    if audio_map == "0:a:0":
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    else:
        pid = int(audio_map.split(":")[-1], 16)
        audio = next((s for s in streams if s.get("codec_type") == "audio" and _stream_pid(s) == pid), None)
    if audio is None or not audio.get("sample_rate"):
        raise FFprobeError("E102", key="exceptions.script_audio_missing", path=source)

    video_index = int(video["index"])
    fps = file_info.fps
    pictures = ffmpeg.get_stream_packets(source, [video_index], frame_rates={video_index: fps})[video_index]
    if not pictures:
        raise FFprobeError("E102", key="exceptions.no_video_stream", path=source)
    video_start = float(video.get("start_time") or 0.0)
    last_pts, last_duration = pictures[-1]
    end_time = last_pts + (last_duration or 1.0 / fps)
    ranges = frame_ranges(file_info.cuts, video_start, fps, end_time, ffmpeg.get_first_video_frame_pts(source))
    if not ranges:
        raise ValidationError("E201", key="exceptions.no_keep_ranges")
    top_field_first = ffmpeg.get_source_timing(source).top_field_first
    fields = field_numbers(ranges, [pts for pts, _ in pictures], video_start, fps, top_field_first)
    sample_rate = int(audio["sample_rate"])
    format_start = float(data.get("format", {}).get("start_time") or 0.0)
    return ScriptPlan(ranges=ranges, fields=fields, fps=fps, top_field_first=top_field_first,
                      audio_segments=audio_segments(ranges, video_start, fps, sample_rate),
                      sample_rate=sample_rate, format_start=format_start, audio_probe_args=list(probe_args),
                      audio_map=audio_map)


def _stream_pid(stream: dict) -> Optional[int]:
    try:
        return int(str(stream.get("id")), 16)
    except ValueError:
        return None
