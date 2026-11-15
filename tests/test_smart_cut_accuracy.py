"""カット結果の正確さの回帰テスト（実ffmpeg、放送TSに近い素材）。

2026-10-04に見つかった次の不具合が再発しないことを、出力の中身で確かめる:
- TSの入力シークが次のキーフレームまで行き過ぎ、Headが別のフレームになる
- 再エンコード部分がインターレースからプログレッシブに変わる
- コピーした音声が区間より手前から始まり、CM音声の混入・音声の重複・
  つなぎ目の映像の時刻の空きが起きる
- オープンGOPのBフレームがBodyの両端で欠ける/壊れる
(末尾の無駄な再エンコードの回避はtest_smart_cut_planning.pyで確認する)
"""

import subprocess

import pytest

from tests._media_check import (
    check_cut,
    display_field_sources,
    frame_scan_flags,
    make_interlaced_source,
    make_mixed_scan_source,
    make_soft_telecine_source,
)
from ts_smartcut.ffmpeg.smart_cut import (
    MODE_FULL_REENCODE,
    MODE_SIMPLE_COPY,
    MODE_SMART_CUT,
    SmartCutEngine,
)
from ts_smartcut.ffmpeg.gpu import choose_encoder, detect_hardware_encoders
from ts_smartcut.errors import MuxFailedError
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper
from ts_smartcut.models import AppSettings, CutRange
from ts_smartcut.system.temp_file_manager import TempFileManager

def _decode_times(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True,
    ).stdout
    rows = [line.strip().rstrip(",").split(",") for line in out.splitlines() if line.strip()]
    return [(float(p), float(d)) for p, d in rows if d != "N/A"]


# 先頭・中間・末尾近くのカット。108以降は動画の末尾まで続く区間になる。
# 108のHeadは2秒以上先なので、粗いシーク(手前のキーフレーム)も通る。
CUTS = [CutRange(20, 40), CutRange(70, 85), CutRange(100, 108)]


@pytest.fixture(scope="module")
def mpeg2_open_gop(tmp_path_factory):
    # 地デジに近い: MPEG-2・インターレース(TFF)・Bフレーム2枚のオープンGOP・
    # AC-3、さらに音声が映像より0.4秒先に始まる。
    return make_interlaced_source(
        tmp_path_factory.mktemp("acc") / "mpeg2.ts",
        ["-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-bf", "2", "-g", "15", "-b:v", "4M"],
        "ac3", audio_lead=0.4,
    )


@pytest.fixture(scope="module")
def h264_interlaced(tmp_path_factory):
    return make_interlaced_source(
        tmp_path_factory.mktemp("acc") / "h264.ts",
        ["-c:v", "libx264", "-flags", "+ilme+ildct", "-bf", "3", "-g", "30", "-preset", "veryfast"],
        "aac",
    )


def _assert_encoder(engine, use_gpu, caplog):
    # GPUの時は、ソフトウェアへのやり直しなしにGPUで符号化したこと
    used = engine._encoder_cache.get("mpeg2video", "mpeg2video")
    assert (used != "mpeg2video") == use_gpu
    assert "redoing it in software" not in caplog.text


@pytest.fixture(scope="module")
def mpeg2_field_gpu():
    """このPCで、MPEG-2をインターレースのまま符号化できるGPUの判定結果
    (Intel QSVのmpeg2_qsv等)。無ければNone(そのテストはskip)。"""
    detection = detect_hardware_encoders()
    return detection if choose_encoder(detection, "mpeg2", interlaced=True) != "mpeg2video" else None


def _hardware(request, use_gpu):
    if not use_gpu:
        return None
    detection = request.getfixturevalue("mpeg2_field_gpu")
    if detection is None:
        pytest.skip("MPEG-2をインターレースで符号化できるGPUが無い")
    return detection


def _process(source, mode, tmp_path):
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    output = tmp_path / f"out_{mode}.ts"
    engine.process(file_info, CUTS, output, mode=mode)
    return check_cut(source, output)


def _expected_frames(total: int):
    return [i for i in range(total) if not any(c.start_frame <= i < c.end_frame for c in CUTS)]


def _assert_audio_ok(result):
    assert result.audio_matched > 0
    assert result.audio_leaked == 0, "カット区間の音声が混入している"
    assert result.audio_duplicated == 0, "同じ音声が二重に入っている"
    # copyはパケット単位なので最大1パケット(AC-3で32ms)未満のずれは許容
    assert result.audio_max_sync_error < 0.032


@pytest.mark.parametrize("source_name", ["mpeg2_open_gop", "h264_interlaced"])
@pytest.mark.parametrize("mode", [MODE_SMART_CUT, MODE_FULL_REENCODE])
def test_frame_accurate_modes_keep_exact_frames_interlace_and_sync(request, tmp_path, source_name, mode):
    source = request.getfixturevalue(source_name)
    result = _process(source, mode, tmp_path)

    assert result.source_frames == _expected_frames(result.source_frame_count)
    assert result.all_interlaced_tff, "インターレース(TFF)が保たれていない"
    assert result.max_pts_step_error < 0.001, "つなぎ目でフレームの時刻に空き/重なりがある"
    _assert_audio_ok(result)


@pytest.mark.parametrize("source_name", ["mpeg2_open_gop", "h264_interlaced"])
def test_nut_audio_fallback_keeps_exact_frames_and_sync(request, tmp_path, monkeypatch, caplog, source_name):
    # 音声をMatroskaへ書けない録画(壊れたADTSヘッダー、2026-10-10)は、その録画の音声を
    # すべてNUTで書き直す。NUTでも映像・音声の位置がMatroskaと同じであること
    source = request.getfixturevalue(source_name)
    original = SmartCutEngine._render_audio_runs

    def mka_fails(self, file_info, audio_runs, suffix, *args, **kwargs):
        if suffix == ".mka":
            raise MuxFailedError("E101", "aac_adtstoasc: Error parsing ADTS frame header!")
        return original(self, file_info, audio_runs, suffix, *args, **kwargs)

    monkeypatch.setattr(SmartCutEngine, "_render_audio_runs", mka_fails)
    with caplog.at_level("WARNING"):
        result = _process(source, MODE_SMART_CUT, tmp_path)
    assert "writing it as NUT instead" in caplog.text
    assert result.source_frames == _expected_frames(result.source_frame_count)
    assert result.max_pts_step_error < 0.001
    _assert_audio_ok(result)


@pytest.mark.parametrize("source_name", ["mpeg2_open_gop", "h264_interlaced"])
def test_simple_copy_cuts_on_keyframe_boundaries_without_broken_frames(request, tmp_path, source_name):
    source = request.getfixturevalue(source_name)
    keyframes = set(FFmpegWrapper().get_keyframes(source, fps=30000 / 1001))
    result = _process(source, MODE_SIMPLE_COPY, tmp_path)

    frames = result.source_frames
    assert frames == sorted(set(frames)), "重複または順序の乱れたフレームがある"
    assert not any(c.start_frame <= f < c.end_frame for f in frames for c in CUTS)
    # 各連続区間はキーフレームから始まる
    run_starts = [f for i, f in enumerate(frames) if i == 0 or frames[i - 1] != f - 1]
    assert all(start in keyframes for start in run_starts)
    assert result.all_interlaced_tff
    assert result.max_pts_step_error < 0.001
    _assert_audio_ok(result)



# ---- ソフトテレシネ/走査方式の混在 (CS-Aの実録画で見つかった問題、2026-10-04) ----------

SCAN_CUTS = [CutRange(20, 55), CutRange(65, 100), CutRange(130, 150)]


@pytest.fixture(scope="module")
def soft_telecine(tmp_path_factory):
    return make_soft_telecine_source(tmp_path_factory.mktemp("acc") / "soft.ts")


@pytest.fixture(scope="module")
def mixed_scan(tmp_path_factory):
    # インターレース → ソフトテレシネ → インターレース
    return make_mixed_scan_source(tmp_path_factory.mktemp("acc") / "mixed.ts")


def test_soft_telecine_is_not_misdetected_as_vfr(soft_telecine):
    # ffprobeはr_frame_rate=59.94(フィールド), avg=29.97と報告する。VFRと誤判定して
    # Smart Cutを無効にし、fpsも59.94と誤認していた。
    wrapper = FFmpegWrapper()
    assert wrapper.is_vfr(soft_telecine) is False
    assert wrapper.get_file_info(soft_telecine).fps == pytest.approx(30000 / 1001)
    # 先頭のfield_orderは"progressive"だが、フィールド単位の映像として扱う
    assert wrapper.get_source_timing(soft_telecine).interlaced is True


@pytest.mark.parametrize("use_gpu", [False, True], ids=["software", "gpu"])
@pytest.mark.parametrize("source_name", ["soft_telecine", "mixed_scan"])
def test_smart_cut_preserves_each_frames_scan_flags_and_displayed_fields(request, tmp_path, source_name, use_gpu, caplog):
    # 再エンコード部分も、元の各フレームの走査情報(プログレッシブ/インターレース、
    # トップフィールドが先か、フィールドを繰り返すか)をそのまま保つ。ffmpegの
    # MPEG-2エンコーダーは繰り返し指示を書けないため、エンコード後にビットを
    # 書き戻している(ffmpeg/mpeg2_flags.py)。
    source = request.getfixturevalue(source_name)
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    output = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings(), _hardware(request, use_gpu))
    mode, _ = engine.process(file_info, SCAN_CUTS, output, mode=MODE_SMART_CUT)
    _assert_encoder(engine, use_gpu, caplog)
    assert mode == MODE_SMART_CUT

    fps = file_info.fps
    video_start = ffmpeg.get_source_timing(source).video_start
    keep, cursor = [], 0
    for cut in SCAN_CUTS:
        if cut.start_frame > cursor:
            keep.append((cursor, cut.start_frame))
        cursor = cut.end_frame
    keep.append((cursor, 10 ** 9))
    expected_flags = [
        flags for t, flags in frame_scan_flags(source)
        if any(video_start + (s - 0.5) / fps <= t < video_start + (e - 0.5) / fps for s, e in keep)
    ]
    output_frames = frame_scan_flags(output)
    assert [flags for _, flags in output_frames] == expected_flags

    # 時刻はフレームごとに2または3フィールド分ずつ進み、空き・重なりがない
    field = 1 / (2 * fps)
    steps = {round((b - a) / field, 2) for (a, _), (b, _) in zip(output_frames, output_frames[1:])}
    assert steps <= {2.0, 3.0}

    expected_fields, actual_fields = display_field_sources(
        source, output, [(c.start_frame, c.end_frame) for c in SCAN_CUTS], fps
    )
    assert actual_fields == expected_fields

    result = check_cut(source, output)
    assert result.audio_leaked == 0 and result.audio_duplicated == 0
    assert result.audio_max_sync_error < 0.032


def test_single_frame_reencoded_part_keeps_timestamps_continuous(mixed_scan, tmp_path):
    # キーフレームの1フレーム手前から残すと、Headが1フレームだけになる。1枚だけの
    # 再エンコード部分でデコード時刻が失われ、連結後に時刻が約1.4秒戻った
    # (BS-Aの実録画で確認)。
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(mixed_scan)
    keyframes = ffmpeg.get_keyframes(mixed_scan, fps=file_info.fps)
    keyframe = next(k for k in keyframes if k >= 30)
    output = tmp_path / "one.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    engine.process(file_info, [CutRange(10, keyframe - 1)], output, mode=MODE_SMART_CUT)

    frames = frame_scan_flags(output)
    field = 1 / (2 * file_info.fps)
    assert {round((b - a) / field, 2) for (a, _), (b, _) in zip(frames, frames[1:])} <= {2.0, 3.0}
    # カット直前のTail(再エンコード)→1フレームのHead(再エンコード)→コピー部分と
    # 続く所でも、デコード時刻の逆転(1/90000秒の辻褄合わせ)が起きない。Head
    # だけでは前倒しの余地が足りず、前のTailへ受け渡す必要があった(BS-Aで確認)。
    times = _decode_times(output)
    assert min(b[1] - a[1] for a, b in zip(times, times[1:])) > 0.001


@pytest.mark.parametrize("source_name", ["mixed_scan", "mpeg2_open_gop"])
def test_decode_times_stay_ordered_across_reencoded_to_copy_joins(request, tmp_path, source_name):
    # Bフレームなしで再エンコードした部分の後に、元のBフレーム入りのコピー部分が
    # 続くと、コピー部分の先頭のデコード時刻が前の部分の最後より前になる逆転が
    # 起き、ffmpegが1/90000秒だけずらして辻褄を合わせていた(実録画で確認)。
    source = request.getfixturevalue(source_name)
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    output = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    engine.process(file_info, SCAN_CUTS, output, mode=MODE_SMART_CUT)

    times = _decode_times(output)
    gaps = [b[1] - a[1] for a, b in zip(times, times[1:])]
    assert min(gaps) > 0.001  # 1/90000秒の「辻褄合わせ」が無い
    assert all(d <= p + 1e-6 for p, d in times)


@pytest.mark.parametrize("cut_start", [45, 46, 47])
def test_segment_ending_on_or_just_after_keyframe_keeps_decode_times_apart(mpeg2_open_gop, tmp_path, cut_start):
    # 残す区間がキーフレーム45ちょうど(か直後)で終わると、Tailがオープン
    # GOPのリーディングピクチャ2〜4枚だけになり、copyに挟まれてデコード時刻が
    # 1フィールド未満(45ちょうどでは0.01ms)に詰まっていた(2026-10-05)。
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(mpeg2_open_gop)
    output = tmp_path / "out.ts"
    cuts = [CutRange(cut_start, 75)]
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    engine.process(file_info, cuts, output, mode=MODE_SMART_CUT)

    field = 1 / (2 * file_info.fps)
    times = _decode_times(output)
    assert min(b[1] - a[1] for a, b in zip(times, times[1:])) >= field - 1e-4
    result = check_cut(mpeg2_open_gop, output)
    assert result.source_frames == [i for i in range(result.source_frame_count) if not cut_start <= i < 75]
    assert result.max_pts_step_error < 0.001


def test_tail_read_is_retried_then_falls_back_to_the_whole_file(tmp_path):
    # コピー部分の末尾を読むTSのシーク読みは、まれにエラーなしで何も返さなかった
    # (テスト用の映像で約300回に1回、2026-10-09)。つなぎ目の調整が黙って飛ばされ、
    # デコード時刻が1/90000秒に詰まっていた。1回読み直し、それでも空なら全体を読む。
    engine = SmartCutEngine(FFmpegWrapper(), TempFileManager(tmp_path / "temp"), AppSettings())
    whole = [(10.0, 9.9), (10.1, 10.0), (19.9, 19.8)]
    calls = []

    def fake_order(path, read_intervals=None):
        calls.append(read_intervals)
        if read_intervals == "%+#1":
            return 10.0, whole[:1]
        if read_intervals and read_intervals.endswith("%"):
            return 10.0, []  # 末尾のシーク読みが空
        return 10.0, whole

    engine.ffmpeg.get_video_decode_order = fake_order
    assert engine._read_tail_packets(tmp_path / "part.ts", 10.0) == whole
    tail_reads = [c for c in calls if c and c.endswith("%") and c != "%+#1"]
    assert tail_reads == ["17.000%", "17.000%"]  # 末尾から3秒を2回
    assert calls[-1] is None  # 最後に全体

    # 1回目が空でも2回目に返れば全体は読まない
    calls.clear()
    results = iter([[], whole[-1:]])

    def empty_once(path, read_intervals=None):
        calls.append(read_intervals)
        return 10.0, whole[:1] if read_intervals == "%+#1" else next(results)

    engine.ffmpeg.get_video_decode_order = empty_once
    assert engine._read_tail_packets(tmp_path / "part.ts", 10.0) == whole[-1:]
    assert None not in calls


def _drop_audio_ts_packets(source: Path, output: Path, count: int = 3) -> None:
    """受信エラー(ドロップ)と同じく、途中の音声のTSパケットをcount個欠けさせる。"""
    data = source.read_bytes()
    packets = [data[i:i + 188] for i in range(0, len(data), 188)]
    audio = [i for i, p in enumerate(packets) if ((p[1] & 0x1F) << 8) | p[2] == 0x101]  # ffmpegの音声PID
    drop = set(audio[len(audio) // 2:len(audio) // 2 + count])
    output.write_bytes(b"".join(p for i, p in enumerate(packets) if i not in drop))


@pytest.mark.parametrize("mode", [MODE_SMART_CUT, MODE_FULL_REENCODE])
def test_corrupt_aac_packets_from_reception_errors_do_not_fail_the_cut(tmp_path, mode):
    # ドロップで壊れたAACパケットがあると、音声をMatroskaへ切り出す時の
    # aac_adtstoascが止まり、録画全体がE101になっていた(CS-Bの
    # _drop録画6本、2026-10-10)。壊れたパケットは捨てて続ける。
    clean = tmp_path / "clean.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=6:s=320x240:r=30000/1001",
                    "-f", "lavfi", "-i", "sine=f=440:d=6", "-c:v", "mpeg2video", "-g", "15", "-c:a", "aac",
                    "-f", "mpegts", str(clean)], check=True)
    source = tmp_path / "drop.ts"
    _drop_audio_ts_packets(clean, source)
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    output = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    engine.process(file_info, [CutRange(20, 40)], output, mode=mode)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            str(output)], capture_output=True, text=True).stdout
    assert "video" in probe and "audio" in probe


def test_unreadable_decode_times_are_logged_and_the_cut_still_finishes(mpeg2_open_gop, tmp_path, caplog):
    # デコード時刻を読めない部分があっても出力は作る(そのつなぎ目は調整しない)。
    # 以前は警告なしに調整を飛ばしていた。
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(mpeg2_open_gop)
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    ffmpeg.get_video_decode_order = lambda path, read_intervals=None: (None, [])
    output = tmp_path / "out.ts"
    with caplog.at_level("WARNING"):
        engine.process(file_info, [CutRange(47, 75)], output, mode=MODE_SMART_CUT)
    assert "Could not read the decode times used to fit a join" in caplog.text
    result = check_cut(mpeg2_open_gop, output)
    assert result.source_frames == [i for i in range(result.source_frame_count) if not 47 <= i < 75]


@pytest.mark.parametrize("mode", [MODE_SMART_CUT, MODE_FULL_REENCODE])
def test_recording_starting_mid_gop_keeps_its_start(mpeg2_open_gop, tmp_path, mode):
    # 録画はGOPの途中から始まり、最初のキーフレームより前のフレームはデコード
    # できない。先頭を残すと、Headの再エンコードで元フレームが見つからずE101で
    # 止まっていた(BS-Aの実録画、2026-10-05)。デコードできる最初のフレームから
    # 出力する。
    source = tmp_path / "mid_gop.ts"
    # 先頭のIフレームを含む4パケットを落とし、次のキーフレームまでデコードできない
    # 先頭を作る(GOPの途中から始まった録画と同じ状態)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(mpeg2_open_gop), "-map", "0", "-c", "copy",
                    "-bsf:v", r"noise=drop=lt(n\,4)", str(source)], check=True)
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    timing = ffmpeg.get_source_timing(source)
    first = round((ffmpeg.get_first_video_frame_pts(source) - timing.video_start) * file_info.fps)
    assert first > 0, "素材がGOPの途中から始まっていない"

    output = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    engine.process(file_info, [CutRange(50, 70)], output, mode=mode)

    result = check_cut(source, output)
    # check_cutの番号はデコードできたフレームの順番(=フレーム番号-first)
    assert result.source_frames == [
        i for i in range(result.source_frame_count) if not 50 <= first + i < 70
    ]
    assert result.max_pts_step_error < 0.001
    _assert_audio_ok(result)


@pytest.mark.parametrize("mode", [MODE_SMART_CUT, MODE_FULL_REENCODE])
def test_cut_leaving_only_frames_past_the_real_video_end(mpeg2_open_gop, tmp_path, mode):
    # 音声が映像より長いTSではtotal_framesが全体の長さからの見積もりで実際より
    # 多い。末尾近くまでカットすると、残る区間が存在しないフレームを指し、
    # 再エンコードでE101になっていた(BS-Aの実録画、2026-10-05)。
    source = tmp_path / "long_audio.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(mpeg2_open_gop),
                    "-f", "lavfi", "-i", "anoisesrc=d=7:c=pink:r=48000:seed=3",
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "ac3", str(source)], check=True)
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    real_frames = check_cut(source, source).source_frame_count
    assert file_info.total_frames > real_frames + 1, "total_framesが多めに見積もられていない"

    output = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings())
    engine.process(file_info, [CutRange(100, file_info.total_frames - 1)], output, mode=mode)

    assert check_cut(source, output).source_frames == list(range(100))


# ---- Full Re-encodeも元ソースの構造を保つ ------------------------------------------

@pytest.mark.parametrize("use_gpu", [False, True], ids=["software", "gpu"])
@pytest.mark.parametrize("source_name", ["soft_telecine", "mixed_scan"])
def test_full_reencode_preserves_each_frames_scan_flags(request, tmp_path, source_name, use_gpu, caplog):
    # Full Re-encodeでも、元と同じコーデック(MPEG-2)で、各フレームの走査情報
    # (プログレッシブ/インターレース、フィールド順、フィールド繰り返し)を保つ。
    source = request.getfixturevalue(source_name)
    ffmpeg = FFmpegWrapper()
    file_info = ffmpeg.get_file_info(source)
    output = tmp_path / "full.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings(), _hardware(request, use_gpu))
    engine.process(file_info, SCAN_CUTS, output, mode=MODE_FULL_REENCODE)
    _assert_encoder(engine, use_gpu, caplog)

    fps = file_info.fps
    video_start = ffmpeg.get_source_timing(source).video_start
    keep, cursor = [], 0
    for cut in SCAN_CUTS:
        if cut.start_frame > cursor:
            keep.append((cursor, cut.start_frame))
        cursor = cut.end_frame
    keep.append((cursor, 10 ** 9))
    expected_flags = [
        flags for t, flags in frame_scan_flags(source)
        if any(video_start + (s - 0.5) / fps <= t < video_start + (e - 0.5) / fps for s, e in keep)
    ]
    assert [flags for _, flags in frame_scan_flags(output)] == expected_flags
    expected_fields, actual_fields = display_field_sources(
        source, output, [(c.start_frame, c.end_frame) for c in SCAN_CUTS], fps
    )
    assert actual_fields == expected_fields


def test_unusable_gpu_mpeg2_output_falls_back_to_software(mixed_scan, tmp_path, monkeypatch, caplog):
    # GPUの出力が使えない(ここではGPUのエンコードが失敗する)時は、その範囲を
    # ソフトウェアでやり直し、結果は元の走査情報どおりになる
    from ts_smartcut.errors import MuxFailedError
    from ts_smartcut.ffmpeg.gpu import HardwareDetection, HardwareEncoder

    qsv = HardwareEncoder("qsv", "intel", False, None, {"mpeg2": {"progressive": True, "interlaced": True}})
    ffmpeg = FFmpegWrapper()
    real_run = ffmpeg.run
    gpu_calls = []

    def run(args, progress_callback=None):
        if "mpeg2_qsv" in args:
            gpu_calls.append(args)
            raise MuxFailedError("E101", "GPUのエンコードに失敗(テスト)")
        return real_run(args, progress_callback=progress_callback)

    monkeypatch.setattr(ffmpeg, "run", run)
    file_info = ffmpeg.get_file_info(mixed_scan)
    output = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings(), HardwareDetection(qsv, [qsv]))
    engine.process(file_info, SCAN_CUTS, output, mode=MODE_FULL_REENCODE)

    assert len(gpu_calls) == 1  # 最初の失敗以降はソフトウェアで符号化する
    assert "redoing it in software" in caplog.text
    video_start = ffmpeg.get_source_timing(mixed_scan).video_start
    fps = file_info.fps
    keep, cursor = [], 0
    for cut in SCAN_CUTS:
        if cut.start_frame > cursor:
            keep.append((cursor, cut.start_frame))
        cursor = cut.end_frame
    keep.append((cursor, 10 ** 9))
    expected = [
        flags for t, flags in frame_scan_flags(mixed_scan)
        if any(video_start + (s - 0.5) / fps <= t < video_start + (e - 0.5) / fps for s, e in keep)
    ]
    assert [flags for _, flags in frame_scan_flags(output)] == expected
