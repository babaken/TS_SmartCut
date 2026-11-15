import json
import subprocess

import pytest

from ts_smartcut.errors import UnsupportedCodecError, ValidationError
from ts_smartcut.ffmpeg.smart_cut import (
    MODE_FULL_REENCODE,
    MODE_SIMPLE_COPY,
    MODE_SMART_CUT,
    SegmentPart,
    SmartCutEngine,
)
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper
from ts_smartcut.models import AppSettings, CutRange
from ts_smartcut.system.temp_file_manager import TempFileManager


@pytest.fixture
def settings() -> AppSettings:
    # ソフトウェアエンコードを使い、テストを高速化する設定。
    return AppSettings()


@pytest.fixture
def ffmpeg() -> FFmpegWrapper:
    return FFmpegWrapper()


@pytest.fixture
def engine(ffmpeg, settings, tmp_path) -> SmartCutEngine:
    temp_mgr = TempFileManager(tmp_path / "ts_smartcut_temp")
    return SmartCutEngine(ffmpeg, temp_mgr, settings)


@pytest.fixture(scope="module")
def sample_mpeg2_video(tmp_path_factory):
    # MPEG-2映像+AC-3音声の合成TS(sample_videoのH.264/AAC版と同じ尺・GOP・
    # カットパラメータになるよう揃えてある: 3秒・25fps・GOP10・75フレーム)。
    # TS放送録画(本アプリの主用途)を模したソースコーデックの回帰テスト用。
    out_dir = tmp_path_factory.mktemp("ffmpeg_mpeg2_fixtures")
    path = out_dir / "sample_mpeg2.ts"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
            "-c:v", "mpeg2video", "-g", "10",
            "-c:a", "ac3",
            "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _assert_decodes_cleanly_with_frame_count(path, expected_frames: int) -> None:
    """process()が例外を出さないことだけでは、コーデック混在による出力破損
    (実機検証で発見: H.264とMPEG-2が混在したストリームでも例外は出ない)を
    検知できない。実際にデコードしてエラーが出ないこと、正しいフレーム数で
    あることを直接確認する。

    "non monotonically increasing dts"警告はconcat結合された音声ストリーム
    の既知の軽微な副作用(このテストの対象であるコーデック混在破損とは無関係
    — 正常なH.264ケースでは出ないがMPEG-2/AC-3ケースでは出ることを確認済み)
    のため許容し、それ以外の行が1つでもあれば失敗とする。"""
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert decode.returncode == 0, f"decode failed: {decode.stderr}"
    _benign_markers = ("non monotonically increasing dts", "Last message repeated")
    unexpected_lines = [
        line for line in decode.stderr.splitlines()
        if line.strip() and not any(marker in line for marker in _benign_markers)
    ]
    assert unexpected_lines == [], f"decode produced errors: {unexpected_lines}"

    # -of csv/defaultはTSファイルだと"programs"と"streams"の両方に同じ
    # ストリームが列挙され行が重複することがある(実機で確認)。JSONで
    # トップレベルのstreams配列から一意に取り出す。
    count = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries", "stream=nb_read_frames",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(count.stdout)["streams"]
    assert len(streams) == 1
    assert int(streams[0]["nb_read_frames"]) == expected_frames


def test_smart_cut_removes_middle_segment(engine, ffmpeg, sample_video, tmp_path):
    file_info = ffmpeg.get_file_info(sample_video)
    output = tmp_path / "smart_cut_out.mp4"

    events = []
    mode, warning = engine.process(
        file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT,
        progress_callback=events.append,
    )

    assert mode == MODE_SMART_CUT
    assert warning is None
    assert output.exists()

    out_info = ffmpeg.get_file_info(output)
    # 残す区間: [0,30) + [45,75) = 60フレーム、フレーム精度で一致するはず
    assert out_info.total_frames == 60
    assert any(e.phase in ("Head", "Body", "Tail") for e in events)


def test_join_works_in_folders_with_quotes_and_symbols(ffmpeg, settings, sample_video, tmp_path):
    # 部品は concat の一覧に '…' で書く。フォルダ名に ' があると結合が必ず失敗していた
    # (2026-10-22)。他の記号・空白・日本語はそのままで読める
    folder = tmp_path / "it's !#$%&()[]{}~ 録画"
    engine = SmartCutEngine(ffmpeg, TempFileManager(folder / "temp"), settings)
    file_info = ffmpeg.get_file_info(sample_video)
    output = folder / "out.mp4"

    engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT)

    assert ffmpeg.get_file_info(output).total_frames == 60


def test_simple_copy_snaps_to_keyframes_and_loses_a_little_precision(engine, ffmpeg, sample_video, tmp_path):
    file_info = ffmpeg.get_file_info(sample_video)
    output = tmp_path / "simple_copy_out.mp4"

    mode, warning = engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SIMPLE_COPY)

    assert mode == MODE_SIMPLE_COPY
    assert warning is None
    assert output.exists()

    out_info = ffmpeg.get_file_info(output)
    # 2番目の区間の開始位置が45->50(直後のキーフレーム)へスナップされ、
    # 5フレーム分短くなる: 30 + (75-50) = 55フレーム。
    assert out_info.total_frames == 55


def test_full_reencode_is_frame_accurate(engine, ffmpeg, sample_video, tmp_path):
    file_info = ffmpeg.get_file_info(sample_video)
    output = tmp_path / "full_reencode_out.mp4"

    mode, warning = engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_FULL_REENCODE)

    assert mode == MODE_FULL_REENCODE
    assert warning is None
    assert output.exists()

    out_info = ffmpeg.get_file_info(output)
    assert out_info.total_frames == 60


def test_smart_cut_no_cuts_preserves_full_duration(engine, ffmpeg, sample_video, tmp_path):
    file_info = ffmpeg.get_file_info(sample_video)
    output = tmp_path / "no_cuts_out.mp4"

    mode, warning = engine.process(file_info, [], output, mode=MODE_SMART_CUT)

    assert mode == MODE_SMART_CUT
    assert warning is None
    assert output.exists()
    out_info = ffmpeg.get_file_info(output)
    assert out_info.total_frames == file_info.total_frames


def test_smart_cut_entire_file_cut_raises_validation_error(engine, ffmpeg, sample_video, tmp_path):
    file_info = ffmpeg.get_file_info(sample_video)
    output = tmp_path / "should_not_exist.mp4"

    with pytest.raises(ValidationError) as exc_info:
        engine.process(
            file_info, [CutRange(0, file_info.total_frames)], output, mode=MODE_SMART_CUT
        )
    assert exc_info.value.code == "E201"
    assert not output.exists()


def test_vfr_source_forces_simple_copy_with_warning(engine, ffmpeg, sample_video, tmp_path, monkeypatch):
    file_info = ffmpeg.get_file_info(sample_video)
    monkeypatch.setattr(ffmpeg, "is_vfr", lambda path: True)

    output = tmp_path / "vfr_forced_out.mp4"
    mode, warning = engine.process(
        file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT
    )

    assert mode == MODE_SIMPLE_COPY
    assert warning is not None
    assert output.exists()


def test_non_smart_cut_modes_do_not_probe_vfr(engine, ffmpeg, sample_video, tmp_path, monkeypatch):
    file_info = ffmpeg.get_file_info(sample_video)

    def _boom(path):
        raise AssertionError("Simple Copy/Full Re-encodeではis_vfrを呼ぶべきではない")

    monkeypatch.setattr(ffmpeg, "is_vfr", _boom)

    output = tmp_path / "simple_copy_no_probe.mp4"
    engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SIMPLE_COPY)
    assert output.exists()


def test_smart_cut_mpeg2_source_head_tail_match_source_codec(
    engine, ffmpeg, sample_mpeg2_video, tmp_path
):
    # 実機検証で発見したバグの回帰テスト: MPEG-2ソースのSmart CutでHead/Tail
    # が(誤って)H.264になると、BodyのMPEG-2(-c copy)と混在したconcat出力が
    # 壊れる(process()自体は例外を出さないが、デコード時に大量のエラーが出て
    # 尺も異常値になる)。ソースと同じコーデック(mpeg2video)でHead/Tailを
    # 再エンコードすればクリーンにデコードできることを確認する。
    file_info = ffmpeg.get_file_info(sample_mpeg2_video)
    assert file_info.video_codec == "mpeg2video"
    output = tmp_path / "smart_cut_mpeg2_out.ts"

    mode, warning = engine.process(
        file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT,
    )

    assert mode == MODE_SMART_CUT
    assert warning is None
    assert output.exists()
    # 残す区間: [0,30) + [45,75) = 60フレーム(sample_videoのH.264版と同じ計算)
    _assert_decodes_cleanly_with_frame_count(output, expected_frames=60)


def test_smart_cut_unsupported_codec_source_raises_unsupported_codec_error(
    engine, ffmpeg, sample_video, tmp_path
):
    file_info = ffmpeg.get_file_info(sample_video)
    file_info.video_codec = "vp9"  # このコーデックに対応するSmart Cut再エンコードは存在しない
    output = tmp_path / "should_not_exist.mp4"

    with pytest.raises(UnsupportedCodecError) as exc_info:
        engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT)

    assert exc_info.value.code == "E302"
    assert not output.exists()


def test_unsupported_codec_works_with_simple_copy_but_not_full_reencode(
    engine, ffmpeg, sample_video, tmp_path
):
    # 再エンコードは常に元と同じコーデックで行うため、Full Re-encodeも対応
    # コーデック(MPEG-2/H.264)に限られる。再エンコードしないSimple Copyは可。
    file_info = ffmpeg.get_file_info(sample_video)
    file_info.video_codec = "vp9"

    output = tmp_path / "simple_copy_unsupported_codec.mp4"
    mode, warning = engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SIMPLE_COPY)
    assert mode == MODE_SIMPLE_COPY
    assert output.exists()

    with pytest.raises(UnsupportedCodecError):
        engine.process(file_info, [CutRange(30, 45)], tmp_path / "full.mp4", mode=MODE_FULL_REENCODE)


def test_single_part_output_preserves_container_format(
    engine, ffmpeg, sample_mpeg2_video, tmp_path
):
    # 実機検証で発見したバグの回帰テスト: 出力パートが1つだけの場合
    # (今回はカット無し = 単一区間)、process()は一時ファイル(常に固定で
    # .mp4として生成されていた)をshutil.move()でoutput_pathへそのまま
    # 移動するだけだった。output_pathの拡張子が.ts/.m2ts等でも中身は
    # MP4コンテナのままという、ファイル名と実体の不整合が起きていた
    # (process()自体は例外を出さないため検知できない)。一時ファイルを
    # output_pathと同じ拡張子で作るよう修正し、実際のコンテナ形式が
    # 拡張子と一致することを確認する。
    file_info = ffmpeg.get_file_info(sample_mpeg2_video)
    output = tmp_path / "single_part.ts"

    mode, warning = engine.process(file_info, [], output, mode=MODE_FULL_REENCODE)

    assert mode == MODE_FULL_REENCODE
    assert output.exists()

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "csv=p=0", str(output)],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "mpegts"


def _capture_reencode_args(engine, ffmpeg, monkeypatch):
    """再エンコード(action="reencode")のffmpeg呼び出し引数を捕捉する
    (copyアクション(Body)は-c copyのみで呼ばれるため対象外)。実際の
    ffmpeg実行はそのまま行う(差し替えると後続のshutil.move()/concatが
    実ファイル不在で失敗するため、スパイとして元のrunを呼び出す)。"""
    captured = []
    original_run = ffmpeg.run

    def _spy_run(args, progress_callback=None):
        if "-c:v" in args:
            captured.append(args)
        return original_run(args, progress_callback=progress_callback)

    monkeypatch.setattr(ffmpeg, "run", _spy_run)
    return captured


def test_smart_cut_uses_source_bitrate_not_crf(
    engine, ffmpeg, sample_mpeg2_video, tmp_path, monkeypatch
):
    file_info = ffmpeg.get_file_info(sample_mpeg2_video)
    assert file_info.bitrate > 0
    captured = _capture_reencode_args(engine, ffmpeg, monkeypatch)

    output = tmp_path / "bitrate_match.ts"
    engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT)

    assert captured, "再エンコードが1回も発生しなかった"
    for args in captured:
        assert "-b:v" in args
        assert str(file_info.bitrate) in args
        assert "-maxrate" in args
        assert "-bufsize" in args
        assert "-crf" not in args


def test_falls_back_to_fixed_quality_when_bitrate_unknown(
    engine, ffmpeg, sample_mpeg2_video, tmp_path, monkeypatch
):
    file_info = ffmpeg.get_file_info(sample_mpeg2_video)
    file_info.bitrate = 0
    captured = _capture_reencode_args(engine, ffmpeg, monkeypatch)

    output = tmp_path / "bitrate_unknown.ts"
    engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_SMART_CUT)

    assert captured, "再エンコードが1回も発生しなかった"
    for args in captured:
        assert "-b:v" not in args
        # mpeg2videoは-crfではなく-q:vにフォールバックする
        assert "-q:v" in args or "-crf" in args


def test_full_reencode_also_matches_source_bitrate(
    engine, ffmpeg, sample_video, tmp_path, monkeypatch
):
    # 画質は元ソースに合わせる(利用者が選ぶ設定はない)。Full Re-encodeも同じ。
    file_info = ffmpeg.get_file_info(sample_video)
    assert file_info.bitrate > 0
    captured = _capture_reencode_args(engine, ffmpeg, monkeypatch)

    output = tmp_path / "full_reencode_bitrate.mp4"
    engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_FULL_REENCODE)

    assert captured, "再エンコードが1回も発生しなかった"
    for args in captured:
        assert args[args.index("-b:v") + 1] == str(file_info.bitrate)
        assert "-crf" not in args


def test_smart_cut_gpu_encoder_gets_bitrate_but_no_preset(
    engine, ffmpeg, sample_mpeg2_video, monkeypatch
):
    # mpeg2_qsv等の実GPUエンコーダーはこのサンドボックス環境に無いため、
    # _render_part()を直接呼び出して引数構築のみを検証する(実エンコードは
    # 行わない)。
    file_info = ffmpeg.get_file_info(sample_mpeg2_video)
    assert file_info.bitrate > 0
    monkeypatch.setattr(engine, "_select_video_encoder", lambda video_codec: "mpeg2_qsv")

    captured = []
    monkeypatch.setattr(ffmpeg, "run", lambda args, progress_callback=None: captured.append(args))

    part = SegmentPart("reencode", "Head", 0, 5)
    engine._render_part(
        file_info, part, sample_mpeg2_video.parent / "gpu_part.ts",
        "mpeg2video", "copy", 0, 1, None, smart_cut=True,
    )

    assert len(captured) == 1
    args = captured[0]
    assert "-c:v" in args and args[args.index("-c:v") + 1] == "mpeg2_qsv"
    assert "-b:v" in args
    assert "-preset" not in args
    assert "-crf" not in args


# ---- Full Re-encodeのコーデック / GPU品質指定 / VAAPI ---------------------

def _video_codec_of(path) -> str:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(probe.stdout)["streams"][0]["codec_name"]


def test_full_reencode_keeps_source_codec(engine, ffmpeg, sample_mpeg2_video, sample_video, tmp_path):
    # Full Re-encodeも元ソースと同じコーデック(MPEG-2→MPEG-2, H.264→H.264)。
    # コーデックの変換はこのツールの目的外。
    for source, codec in ((sample_mpeg2_video, "mpeg2video"), (sample_video, "h264")):
        file_info = ffmpeg.get_file_info(source)
        output = tmp_path / f"full_{codec}{source.suffix}"
        engine.process(file_info, [CutRange(30, 45)], output, mode=MODE_FULL_REENCODE)
        assert _video_codec_of(output) == codec


@pytest.mark.parametrize(
    "encoder,expected_quality",
    [
        ("h264_nvenc", ["-cq", "18"]),
        ("h264_qsv", ["-global_quality", "18"]),
        ("h264_vaapi", ["-qp", "18"]),
    ],
)
def test_gpu_encoder_gets_quality_args_when_bitrate_unknown(
    engine, ffmpeg, sample_video, monkeypatch, encoder, expected_quality
):
    # 元のビットレートが分からない場合だけ固定品質。GPUにも各自の指標で渡す。
    file_info = ffmpeg.get_file_info(sample_video)
    file_info.bitrate = 0
    monkeypatch.setattr(engine, "_select_video_encoder", lambda video_codec: encoder)
    captured = []
    monkeypatch.setattr(ffmpeg, "run", lambda args, progress_callback=None: captured.append(args))

    engine._render_part(
        file_info, SegmentPart("reencode", "Reencode", 0, 5), sample_video.parent / "gpu_q.mp4",
        "libx264", "copy", 0, 1, None,
    )

    args = captured[0]
    flag, value = expected_quality
    assert args[args.index(flag) + 1] == value
    assert "-crf" not in args
    assert "-preset" not in args


def test_vaapi_part_has_device_before_input_and_upload_filter(engine, ffmpeg, sample_video, monkeypatch):
    from ts_smartcut.ffmpeg import gpu as gpu_module

    monkeypatch.setattr(gpu_module, "find_vaapi_device", lambda: "/dev/dri/renderD128")
    file_info = ffmpeg.get_file_info(sample_video)
    monkeypatch.setattr(engine, "_select_video_encoder", lambda video_codec: "h264_vaapi")
    captured = []
    monkeypatch.setattr(ffmpeg, "run", lambda args, progress_callback=None: captured.append(args))

    engine._render_part(
        file_info, SegmentPart("reencode", "Head", 0, 5), sample_video.parent / "vaapi.mp4",
        "libx264", "copy", 0, 1, None, smart_cut=True,
    )

    args = captured[0]
    assert args.index("-vaapi_device") < args.index("-i")
    # 時刻でのフレーム選択(select)の後にGPUへのアップロードが続く
    assert args[args.index("-vf") + 1].endswith(",format=nv12,hwupload")


def _vaapi_h264_works() -> bool:
    from ts_smartcut.ffmpeg.gpu import probe_encoder_functional

    return probe_encoder_functional("h264_vaapi")


@pytest.mark.skipif(not _vaapi_h264_works(), reason="VAAPIでH.264エンコードできるGPUが無い環境")
@pytest.mark.parametrize("mode", [MODE_SMART_CUT, MODE_FULL_REENCODE])
def test_real_vaapi_encode_decodes_cleanly(ffmpeg, sample_video, tmp_path, mode):
    # 実GPU(VAAPI)でのエンドツーエンド確認。Smart CutではHead/TailがGPU
    # (h264_vaapi)、Bodyが元のlibx264ストリームのcopyとなり、両者を連結しても
    # 正しくデコードできることを確認する。
    from ts_smartcut.ffmpeg.gpu import detect_hardware_encoders

    hardware = detect_hardware_encoders()
    if hardware.selected is None or not hardware.selected.supports("h264", interlaced=False):
        pytest.skip("H.264をGPUで符号化できない環境")
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "temp"), AppSettings(), hardware)
    file_info = ffmpeg.get_file_info(sample_video)
    output = tmp_path / f"vaapi_{mode}.mp4"

    engine.process(file_info, [CutRange(33, 45)], output, mode=mode)

    assert engine._select_video_encoder("libx264").endswith(("_vaapi", "_qsv", "_nvenc", "_amf"))
    assert _video_codec_of(output) == "h264"
    _assert_decodes_cleanly_with_frame_count(output, 63)


def test_gpu_used_only_when_it_can_encode_the_needed_scan(engine):
    # 判定済みのGPUがインターレースのまま符号化できない(VAAPI等)なら、
    # インターレースのソースはソフトウェアエンコーダーで処理する。
    from ts_smartcut.ffmpeg.gpu import HardwareDetection, HardwareEncoder
    from ts_smartcut.ffmpeg.wrapper import SourceTiming

    engine.hardware = HardwareDetection(HardwareEncoder(
        "vaapi", "amd", False, "/dev/dri/renderD128",
        {"h264": {"progressive": True, "interlaced": False}, "mpeg2": {"progressive": False, "interlaced": False}},
    ))
    engine._timing = SourceTiming(1.4, 1.4, "tt", True, 0.032)
    assert engine._select_video_encoder("libx264") == "libx264"

    engine._encoder_cache = {}
    engine._timing = SourceTiming(1.4, 1.4, "progressive", True, 0.032)
    assert engine._select_video_encoder("libx264") == "h264_vaapi"
    assert engine._select_video_encoder("mpeg2video") == "mpeg2video"
