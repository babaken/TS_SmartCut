"""出力形式 AVS／VPY (設計書 §6.9): スクリプト・タイムコード・音声(FLAC)。

L-SMASH Works で読む所(フレーム番号が合うか)は tools/check_script_frames.py と実録画で確かめた。ここでは
区間の式・テンプレート・名前の確保と、実 ffmpeg で作った TS から書いた音声の中身を確かめる。
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from ts_smartcut.core import script_output as so
from ts_smartcut.core.batch_manager import STATUS_DONE, STATUS_ERROR, STATUS_IDLE, BatchManager
from ts_smartcut.core.output_manager import OutputManager
from ts_smartcut.errors import FileIOError, ValidationError
from ts_smartcut.ffmpeg.gpu import HardwareDetection
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper
from ts_smartcut.models import AppSettings, BatchItem, CutRange, FileCutInfo

FPS = 30000 / 1001


# ---- 区間とフィールドの式 --------------------------------------------------------


def _field_times(lengths, start: float = 100.0) -> list:
    """絵の表示時刻(各絵が lengths[i] フィールド表示される)。"""
    times, fields = [], 0
    for length in lengths:
        times.append(start + fields / (2 * FPS))
        fields += length
    return times


def _plan(fields, ranges=((0, 1),), top_field_first=True) -> so.ScriptPlan:
    return so.ScriptPlan(ranges=list(ranges), fields=list(fields), fps=FPS, top_field_first=top_field_first,
                         audio_segments=[], sample_rate=48000, format_start=0.0, audio_probe_args=[],
                         audio_map="0:a:0")


def _expand(pieces) -> list:
    return [p.base + p.offsets[i % len(p.offsets)] + (i // len(p.offsets)) * p.cycle
            for p in pieces for i in range(p.length)]


def test_frame_ranges_skip_undecodable_frames_and_the_end_and_merge():
    ranges = so.frame_ranges([CutRange(0, 10), CutRange(10, 20), CutRange(95, 200)], 100.0, FPS,
                             end_time=100.0 + 100 / FPS, first_decodable_pts=100.0 + 3 / FPS)
    assert ranges == [(3, 20), (95, 100)]
    # デコードできる最初の絵がフレームの途中(ソフトテレシネの半フレーム)なら、その次のフレームから
    assert so.frame_ranges([CutRange(0, 10)], 100.0, FPS, 200.0, 100.0 + 2.5 / FPS) == [(3, 10)]
    assert so.frame_ranges([CutRange(20, 30)], 100.0, FPS, end_time=100.0 + 10 / FPS) == []


def test_field_numbers_of_interlaced_pictures_are_the_pictures_in_order():
    times = _field_times([2] * 20)
    assert so.field_numbers([(3, 6)], times, 100.0, FPS, True) == [6, 7, 8, 9, 10, 11]
    # 下のフィールドが先の録画: 各フレームの先が下
    assert so.field_numbers([(3, 4)], times, 100.0, FPS, False) == [7, 6]


def test_field_numbers_repeat_the_fields_of_soft_telecine():
    # 3・2・3・2 フィールドの絵(上下上・下上・下上下・上下)が 29.97 の 5 フレームになる
    times = _field_times([3, 2, 3, 2] * 3)
    assert so.field_numbers([(0, 5)], times, 100.0, FPS, True) == [0, 1, 0, 3, 2, 5, 4, 5, 6, 7]
    # 次の 4 枚も同じ形で 8 つ先
    assert so.field_numbers([(5, 10)], times, 100.0, FPS, True) == [8, 9, 8, 11, 10, 13, 12, 13, 14, 15]


def test_field_numbers_repeat_the_previous_picture_over_a_gap():
    # 受信エラーで 2 枚目と 3 枚目の間の 2 フィールドの絵が抜けた
    times = _field_times([2, 4, 2])
    assert so.field_numbers([(0, 4)], times, 100.0, FPS, True) == [0, 1, 2, 3, 2, 3, 4, 5]


def test_field_pieces_rebuild_the_numbers():
    interlaced = so.field_numbers([(0, 300)], _field_times([2] * 400), 100.0, FPS, True)
    pieces = so.field_pieces(interlaced)
    assert len(pieces) == 1 and pieces[0].contiguous and _expand(pieces) == interlaced

    # インターレース → 3:2 → 抜け → インターレースの録画を、区間の途中(3:2 の途中)から切る
    lengths = [2] * 50 + [3, 2] * 200 + [2, 4] + [2] * 50
    times = _field_times(lengths)
    for start in range(40, 50):
        fields = so.field_numbers([(start, sum(lengths) // 2)], times, 100.0, FPS, True)
        pieces = so.field_pieces(fields)
        assert _expand(pieces) == fields
        assert all(max(p.offsets) < p.cycle for p in pieces)
        assert len(pieces) < 15


def test_audio_segments_keep_the_total_in_step_with_the_video():
    ranges = [(i * 30, i * 30 + 7) for i in range(30)]
    segments = so.audio_segments(ranges, 100.0, FPS, 48000)
    assert sum(n for _, n in segments) == round(30 * 7 / FPS * 48000)
    assert segments[1][0] == pytest.approx(100.0 + 30 / FPS)


def test_audio_filter_keeps_the_odd_segments():
    graph = so.audio_filter([(101.0, 48000), (103.0, 24000)], 100.0, 48000, "0:a:0")
    assert "asegment=samples=48000|96000|144000|168000[s0][s1][s2][s3][s4]" in graph
    assert "[s0]anullsink;[s2]anullsink;[s4]anullsink" in graph
    assert "[s1][s3]concat=n=2:v=0:a=1,apad=whole_len=72000,atrim=end_sample=72000,asetnsamples=n=4608:p=0[out]" in graph
    assert graph.startswith("[0:a:0]asetpts=PTS-100.0/TB,aresample=async=1:min_hard_comp=0.01:first_pts=0,")


def test_timecodes_are_evenly_spaced():
    text = so.timecode_text(3, FPS)
    assert text.splitlines() == ["# timecode format v2", "0.000", "33.367", "66.733"]


def test_fps_fraction():
    assert so.fps_fraction(FPS) == (30000, 1001)
    assert so.fps_fraction(25.0) == (25, 1)


def test_trim_statements():
    # インターレースの 2 区間と、3:2 の区間
    fields = list(range(0, 2)) + list(range(20, 50)) + [0, 1, 0, 3, 2, 5, 4, 5, 6, 7] * 1
    plan = _plan(fields)
    pieces = so.field_pieces(fields)
    assert so.trim_statements(so.OUTPUT_AVS, plan) == (
        "v = v.AssumeTFF().SeparateFields()\n"
        "tssc_fields = " + " ++ ".join(so._avs_piece(p) for p in pieces) + "\n"
        "v = tssc_fields.AssumeFieldBased().AssumeTFF().Weave().AssumeFPS(30000, 1001)"
        '.propSet("_FieldBased", 2)')
    # 下のフィールドが先の録画は、印も下が先
    assert so.trim_statements(so.OUTPUT_AVS, _plan(fields, top_field_first=False)).splitlines()[-1] == (
        'v = tssc_fields.AssumeFieldBased().AssumeBFF().Weave().AssumeFPS(30000, 1001).propSet("_FieldBased", 1)')
    assert so._avs_piece(so.FieldPiece(20, 2, (0, 1), 30)) == "Trim(v, 20, -30)"
    assert so._avs_piece(so.FieldPiece(8, 8, (0, 1, 0, 3, 2, 5, 4, 5, 6, 7), 13)) == (
        "SelectEvery(Trim(v, 8, 0), 8, 0, 1, 0, 3, 2, 5, 4, 5, 6, 7).AssumeFPS(v).Trim(0, -13)")
    assert so._vpy_piece(so.FieldPiece(20, 2, (0, 1), 30)) == "v[20:50]"
    assert so._vpy_piece(so.FieldPiece(8, 8, (0, 1, 0, 3, 2, 5, 4, 5, 6, 7), 13)) == (
        "core.std.SelectEvery(v[8:], 8, [0, 1, 0, 3, 2, 5, 4, 5, 6, 7])[:13]")
    vpy = so.trim_statements(so.OUTPUT_VPY, _plan(fields, top_field_first=False)).splitlines()
    assert vpy[0] == 'v = core.std.SeparateFields(core.std.RemoveFrameProps(v, "_FieldBased"), tff=True)'
    assert vpy[-2:] == ["v = core.std.DoubleWeave(v, tff=False)[::2]",
                        "v = core.std.AssumeFPS(v, fpsnum=30000, fpsden=1001)"]
    # AVS は長い行にしない
    many = so.trim_statements(so.OUTPUT_AVS, _plan([(i * 7919) % 1000 for i in range(400)])).splitlines()
    assert len(many) > 3 and all(line.count("++") <= so._PIECES_PER_LINE for line in many)

# ---- テンプレートと読み込みの式 -----------------------------------------------


def _quoted(output_format: str, path: Path) -> str:
    """スクリプトに書かれる絶対パスの文字列(AVS は "…"、VPY は Python の文字列)。"""
    text = str(path.resolve())
    return so._avs_string(text) if output_format == so.OUTPUT_AVS else so._vpy_string(text)


@pytest.mark.parametrize("output_format", so.SCRIPT_FORMATS)
def test_default_template_renders(output_format, tmp_path):
    video, audio = so.source_expressions(output_format, tmp_path / "rec.ts", tmp_path / "out.flac", tmp_path)
    plan = _plan(list(range(6, 18)), ranges=[(3, 9)])
    script = so.render_script(output_format, so.load_template(output_format, None), video, audio, plan)
    assert "__TSSC_" not in script
    assert _quoted(output_format, tmp_path / "rec.ts") in script
    assert _quoted(output_format, tmp_path / "out.flac") in script
    assert ("repeat=false" in script) if output_format == so.OUTPUT_AVS else ("repeat=0" in script)


def test_template_must_have_every_placeholder():
    with pytest.raises(ValidationError) as e:
        so.check_template("v = __TSSC_VIDEO__\n__TSSC_TRIM__\n")
    assert e.value.code == "E205"
    with pytest.raises(ValidationError):
        so.check_template("__TSSC_VIDEO__ __TSSC_AUDIO__ __TSSC_TRIM__ __TSSC_FPS__")


def test_unreadable_template_is_a_file_error(tmp_path):
    with pytest.raises(FileIOError) as e:
        so.load_template(so.OUTPUT_AVS, tmp_path / "missing.avs")
    assert e.value.code == "E001"


def test_template_with_bom_is_read(tmp_path):
    path = tmp_path / "t.vpy"
    path.write_bytes("\ufeffv = __TSSC_VIDEO__\na = __TSSC_AUDIO__\n__TSSC_TRIM__\n".encode("utf-8"))
    so.check_template(so.load_template(so.OUTPUT_VPY, path))


def test_avs_string_quotes():
    assert so._avs_string("C:\\a b\\c.ts") == '"C:\\a b\\c.ts"'
    assert so._avs_string('/x/a"b.ts') == '"""/x/a"b.ts"""'
    with pytest.raises(ValidationError):
        so._avs_string('/x/a"')


# ---- 名前 ------------------------------------------------------------------


def test_claim_outputs_numbers_all_three_together(tmp_path):
    manager = OutputManager(AppSettings())
    source = tmp_path / "rec.ts"
    source.write_bytes(b"")
    (tmp_path / "cut.flac").write_bytes(b"other")  # 一緒に書くファイルの名前が使われている
    paths = manager.claim_outputs(tmp_path / "cut.avs", source, suffixes=so.COMPANION_SUFFIXES)
    assert [p.name for p in paths] == ["cut_001.avs", "cut_001.timecode.txt", "cut_001.flac"]
    assert all(p.exists() for p in paths)
    assert (tmp_path / "cut.flac").read_bytes() == b"other"


def test_script_names_of_two_batch_items_do_not_share_companions(tmp_path):
    manager = OutputManager(AppSettings())
    source = tmp_path / "rec.ts"
    path = manager.generate_output_path(source, tmp_path / "cut.vpy", reserved=[tmp_path / "cut.avs"],
                                        extension=".vpy")
    assert path.name == "cut_001.vpy"
    assert OutputManager.same_output(tmp_path / "cut.avs", tmp_path / "cut.vpy")
    assert not OutputManager.same_output(tmp_path / "cut.ts", tmp_path / "cut.vpy")
    assert manager.check_output_path(tmp_path / "cut.vpy", source, [tmp_path / "cut.avs"], ".vpy")


def test_check_output_path_reports_an_existing_companion(tmp_path):
    manager = OutputManager(AppSettings())
    (tmp_path / "cut.timecode.txt").write_text("x")
    assert "cut.timecode.txt" in manager.check_output_path(tmp_path / "cut.avs", tmp_path / "rec.ts", [], ".avs")


# ---- バッチ ------------------------------------------------------------------


def _info(path: Path) -> FileCutInfo:
    return FileCutInfo(file_path=path, total_frames=300, fps=FPS, resolution=(320, 240), video_codec="mpeg2video",
                       audio_codec="aac", bitrate=0, duration=10.0, file_size=1, cuts=[CutRange(0, 100)])


def test_set_output_format_changes_the_extension(tmp_path):
    manager = BatchManager(AppSettings(), hardware=HardwareDetection(), record_autocut=False)
    manager.add_item(_info(tmp_path / "a.ts"), output_path=tmp_path / "a_cut.ts")
    assert manager.set_output_format(0, so.OUTPUT_VPY)
    assert manager.items[0].output_path.name == "a_cut.vpy"
    assert manager.items[0].output_format == so.OUTPUT_VPY
    assert manager.set_output_format(0, so.OUTPUT_TS)
    assert manager.items[0].output_path.name == "a_cut.ts"
    assert not manager.set_output_format(0, "mp4")
    manager.items[0].status = STATUS_DONE
    assert not manager.set_output_format(0, so.OUTPUT_AVS)


def test_set_output_format_refuses_a_clash(tmp_path):
    manager = BatchManager(AppSettings(), hardware=HardwareDetection(), record_autocut=False)
    manager.add_item(_info(tmp_path / "a.ts"), output_path=tmp_path / "cut.ts", output_format=so.OUTPUT_AVS)
    manager.items[0].output_path = tmp_path / "cut.avs"
    manager.add_item(_info(tmp_path / "b.ts"), output_path=tmp_path / "cut.ts")
    assert not manager.set_output_format(1, so.OUTPUT_VPY)  # cut.flac が重なる


def test_batch_item_keeps_the_output_format():
    item = BatchItem(order=0, file_info=_info(Path("a.ts")), status=STATUS_IDLE, output_format=so.OUTPUT_AVS)
    assert BatchItem.from_dict(item.to_dict()).output_format == so.OUTPUT_AVS
    data = item.to_dict()
    del data["output_format"]
    assert BatchItem.from_dict(data).output_format == so.OUTPUT_TS


def test_settings_keep_the_templates(tmp_path):
    settings = AppSettings(output_format=so.OUTPUT_VPY, vpy_template=tmp_path / "t.vpy")
    loaded = AppSettings.from_dict(settings.to_dict())
    assert loaded.output_format == so.OUTPUT_VPY
    assert loaded.vpy_template == tmp_path / "t.vpy"
    assert loaded.avs_template is None


# ---- 実 ffmpeg で作った TS から書く --------------------------------------------


@pytest.fixture(scope="module")
def noise_ts(tmp_path_factory) -> Path:
    """10 秒の MPEG-2 + AAC の TS。音声は雑音(どこを切っても位置が一意に分かる)。"""
    path = tmp_path_factory.mktemp("script_output") / "rec.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=10:size=160x120:rate=30000/1001",
         "-f", "lavfi", "-i", "anoisesrc=d=10:r=48000:seed=7", "-map", "0:v", "-map", "1:a",
         "-c:v", "mpeg2video", "-g", "15", "-bf", "2", "-c:a", "aac", "-b:a", "192k", "-output_ts_offset", "100",
         str(path)],
        check=True, capture_output=True,
    )
    return path


def _decode(path: Path, *args: str) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", *args, "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(out, np.float32)


@pytest.mark.parametrize("output_format", so.SCRIPT_FORMATS)
def test_batch_writes_the_script_timecode_and_audio(output_format, noise_ts, tmp_path):
    settings = AppSettings(default_output_dir=tmp_path)
    ffmpeg = FFmpegWrapper(settings)
    info = ffmpeg.get_file_info(noise_ts)
    info.cuts = [CutRange(30, 90), CutRange(150, 240)]
    manager = BatchManager(settings, ffmpeg=ffmpeg, hardware=HardwareDetection(), record_autocut=False)
    extension = so.SCRIPT_EXTENSIONS[output_format]
    assert manager.add_item(info, output_format=output_format, output_path=tmp_path / f"cut{extension}")
    manager.start()
    manager.thread.join()
    item = manager.items[0]
    assert item.status == STATUS_DONE, item.error_message
    script_path = item.output_path
    assert script_path.name == f"cut{extension}"
    timecode = so.companion_path(script_path, so.TIMECODE_SUFFIX)
    flac = so.companion_path(script_path, so.AUDIO_SUFFIX)
    assert timecode.exists() and flac.exists()
    assert not (tmp_path / ".ts_smartcut_temp").exists()

    script = script_path.read_text(encoding="utf-8")
    assert "__TSSC_" not in script
    assert _quoted(output_format, flac) in script
    plan = so.plan_script(ffmpeg, info)
    assert so.trim_statements(output_format, plan) in script
    frames = plan.frame_count
    assert frames == 60 + 90
    assert plan.fields == [f for n in list(range(30, 90)) + list(range(150, 240)) for f in (2 * n, 2 * n + 1)]
    assert len(timecode.read_text().splitlines()) == frames + 1

    # 音声: 長さは映像と同じで、各区間は録画の同じ時刻の音声
    audio = _decode(flac)
    assert len(audio) == round(frames / FPS * 48000)
    source = _decode(noise_ts, "-copyts")  # 録画の音声(時刻 100 秒から)
    source_start = float(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=start_time", "-of", "csv=p=0",
         str(noise_ts)], check=True, capture_output=True, text=True).stdout.split()[0].rstrip(","))
    position = 0
    for (start, samples) in plan.audio_segments:
        piece = audio[position + 4800: position + 9600]
        expected = round((start - source_start) * 48000) + 4800
        window = source[expected - 2400: expected + len(piece) + 2400]
        correlation = np.correlate(window, piece, "valid")
        assert int(np.argmax(correlation)) - 2400 == 0
        position += samples


def test_batch_reports_a_bad_template_before_reading(noise_ts, tmp_path):
    template = tmp_path / "bad.avs"
    template.write_text("v = __TSSC_VIDEO__\n", encoding="utf-8")
    settings = AppSettings(default_output_dir=tmp_path, avs_template=template)
    ffmpeg = FFmpegWrapper(settings)
    info = ffmpeg.get_file_info(noise_ts)
    info.cuts = [CutRange(30, 90)]
    manager = BatchManager(settings, ffmpeg=ffmpeg, hardware=HardwareDetection(), record_autocut=False)
    manager.add_item(info, output_format=so.OUTPUT_AVS, output_path=tmp_path / "cut.avs")
    manager.start()
    manager.thread.join()
    assert manager.items[0].status == STATUS_ERROR
    assert manager.items[0].error_code == "E205"
    assert not list(tmp_path.glob("cut*"))


def test_cancel_removes_the_claimed_files(noise_ts, tmp_path):
    settings = AppSettings(default_output_dir=tmp_path)
    ffmpeg = FFmpegWrapper(settings)
    info = ffmpeg.get_file_info(noise_ts)
    info.cuts = [CutRange(30, 90)]
    manager = BatchManager(settings, ffmpeg=ffmpeg, hardware=HardwareDetection(), record_autocut=False)
    manager.add_item(info, output_format=so.OUTPUT_VPY, output_path=tmp_path / "cut.vpy")
    original = ffmpeg.run

    def cancel_then_run(args, progress_callback=None):
        manager.cancel()
        return original(args, progress_callback)

    ffmpeg.run = cancel_then_run
    manager.start()
    manager.thread.join()
    assert manager.items[0].error_code == "E504"
    assert not list(tmp_path.glob("cut*"))


def test_audio_starting_with_a_tiny_piece_is_written(noise_ts, tmp_path):
    # 区間の境目で切った最初の音声のかたまりが小さい(数サンプル)と、FLAC のエンコーダーがそれをブロックの
    # 大きさにして失敗した(実録画、2026-11-01)。区間の始まりをずらして、いろいろな大きさで書けるか
    ffmpeg = FFmpegWrapper(AppSettings())
    info = ffmpeg.get_file_info(noise_ts)
    for start in range(30, 46):
        info.cuts = [CutRange(start, start + 40)]
        plan = so.plan_script(ffmpeg, info)
        output = tmp_path / f"{start}.flac"
        ffmpeg.run(so.audio_args(plan, noise_ts, output))
        assert len(_decode(output)) == plan.audio_segments[0][1]
