import os
import shutil
import sys
import time

import pytest

from ts_smartcut.errors import FFprobeError, MuxFailedError
from ts_smartcut.ffmpeg import wrapper as wrapper_module
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper, _is_vfr_from_rates


@pytest.fixture
def wrapper() -> FFmpegWrapper:
    return FFmpegWrapper()


def test_get_file_info(wrapper, sample_video):
    info = wrapper.get_file_info(sample_video)
    assert info.file_path == sample_video
    assert info.fps == pytest.approx(25.0)
    assert info.total_frames == 75
    assert info.resolution == (320, 240)
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.duration == pytest.approx(3.0, abs=0.05)
    assert info.file_size == sample_video.stat().st_size
    assert info.bitrate > 0


def test_get_file_info_missing_file_raises_ffprobe_error(wrapper, tmp_path):
    with pytest.raises(FFprobeError):
        wrapper.get_file_info(tmp_path / "does_not_exist.mp4")


def test_get_keyframes(wrapper, sample_video):
    keyframes = wrapper.get_keyframes(sample_video, use_cache=False)
    assert keyframes[0] == 0
    assert keyframes == sorted(keyframes)
    # GOP=10 @ 25fps, 75フレーム分 -> 複数個のキーフレームがあるはず
    assert len(keyframes) >= 5
    for kf in keyframes:
        assert 0 <= kf < 75


def test_get_keyframes_cache_written_and_reused(wrapper, sample_video, tmp_path, monkeypatch):
    """キャッシュは録画の隣ではなく KeyframeCache/ にでき(設計書 §26.1)、2 回目はそれを使う。"""
    cache_dir = tmp_path / "KeyframeCache"
    cache_dir.mkdir()
    monkeypatch.setattr(wrapper_module, "keyframe_cache_dir", lambda: cache_dir)
    local = tmp_path / "rec" / "cached_sample.mp4"
    local.parent.mkdir()
    shutil.copy2(sample_video, local)

    first = wrapper.get_keyframes(local)
    caches = list(cache_dir.glob("*.keyframes.json"))
    assert len(caches) == 1
    assert caches[0].name.startswith("cached_sample.mp4_")
    assert list(local.parent.iterdir()) == [local]  # 録画のフォルダには何も作らない

    def _boom(*args, **kwargs):
        raise AssertionError("キャッシュヒット時はget_file_infoを呼び直してはいけない")

    monkeypatch.setattr(wrapper, "get_file_info", _boom)
    second = wrapper.get_keyframes(local)
    assert second == first

    # 録画を別のフォルダへ移しても(名前・大きさ・更新日時が同じなら)そのまま使える
    moved = tmp_path / "moved" / local.name
    moved.parent.mkdir()
    shutil.move(local, moved)
    assert wrapper.get_keyframes(moved) == first


def test_get_keyframes_cache_invalidated_by_content_change(wrapper, sample_video, tmp_path, monkeypatch):
    cache_dir = tmp_path / "KeyframeCache"
    cache_dir.mkdir()
    monkeypatch.setattr(wrapper_module, "keyframe_cache_dir", lambda: cache_dir)
    local = tmp_path / "cached_sample2.mp4"
    shutil.copy2(sample_video, local)

    first = wrapper.get_keyframes(local)
    with open(local, "ab") as f:
        f.write(b"\0")  # サイズを変えてキャッシュを無効化させる

    calls = []
    original = wrapper.get_file_info
    monkeypatch.setattr(wrapper, "get_file_info", lambda *a, **k: calls.append(1) or original(*a, **k))
    second = wrapper.get_keyframes(local)
    assert calls  # 調べ直した
    assert second == first
    assert len(list(cache_dir.glob("*.keyframes.json"))) == 2  # 変わった録画は別のファイル


def test_keyframe_cache_name_is_short_for_long_japanese_names(tmp_path, monkeypatch):
    """Linux はファイル名が 255 バイトまで。長い番組名でも超えない。"""
    monkeypatch.setattr(wrapper_module, "keyframe_cache_dir", lambda: tmp_path)
    # Linux で作れる最長に近い名前(237 バイト)。そのまま使うとキャッシュ名は 255 バイトを超える
    video = tmp_path / ("名探偵コナン" * 13 + ".ts")
    video.write_bytes(b"x")
    path = wrapper_module._keyframe_cache_path(video, video.stat())
    assert len(path.name.encode("utf-8")) <= 255


def test_unwritable_keyframe_cache_does_not_fail(wrapper, sample_video, tmp_path, monkeypatch):
    """キャッシュを書けなくても録画は開ける(調べた結果を返す)。"""
    missing = tmp_path / "file_not_dir"
    missing.write_text("")
    monkeypatch.setattr(wrapper_module, "keyframe_cache_dir", lambda: missing)
    assert wrapper.get_keyframes(sample_video) == wrapper.get_keyframes(sample_video, use_cache=False)


def test_old_keyframe_caches_are_removed_after_30_days(tmp_path, monkeypatch):
    """最後に使ってから 30 日たったキャッシュと書きかけの一時ファイルだけを消す。
    キャッシュを使うと日付が新しくなる。"""
    monkeypatch.setattr(wrapper_module, "keyframe_cache_dir", lambda: tmp_path)
    now = time.time()
    day = 86400
    old = tmp_path / "old_0123456789abcdef.keyframes.json"
    recent = tmp_path / "recent_0123456789abcdef.keyframes.json"
    old_tmp = tmp_path / "x_0123456789abcdef.keyframes.json.1.2.tmp"
    other = tmp_path / "notes.txt"
    for path, age in ((old, 31), (recent, 29), (old_tmp, 31), (other, 100)):
        path.write_text("{}")
        os.utime(path, (now - age * day, now - age * day))

    assert wrapper_module.remove_old_keyframe_caches(now=now) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([recent.name, other.name])


def test_using_a_keyframe_cache_marks_it_recent(wrapper, sample_video, tmp_path, monkeypatch):
    monkeypatch.setattr(wrapper_module, "keyframe_cache_dir", lambda: tmp_path)
    wrapper.get_keyframes(sample_video)
    (cache,) = tmp_path.glob("*.keyframes.json")
    long_ago = time.time() - 40 * 86400
    os.utime(cache, (long_ago, long_ago))
    wrapper.get_keyframes(sample_video)
    assert cache.stat().st_mtime > time.time() - 60
    assert wrapper_module.remove_old_keyframe_caches() == 0


def test_is_vfr_false_for_constant_frame_rate_sample(wrapper, sample_video):
    assert wrapper.is_vfr(sample_video) is False
    assert wrapper.get_file_info(sample_video).is_vfr is False


@pytest.mark.parametrize(
    "avg,r,expected",
    [
        ("25/1", "25/1", False),
        ("30000/1001", "30000/1001", False),
        ("24/1", "30/1", True),
        ("0/0", "25/1", True),
        ("25/1", "0/1", False),
    ],
)
def test_is_vfr_from_rates_pure_function(avg, r, expected):
    assert _is_vfr_from_rates(avg, r) is expected


def test_run_raises_mux_failed_error_on_nonzero_exit(wrapper):
    with pytest.raises(MuxFailedError) as exc_info:
        wrapper.run(["-i", "/nonexistent/input_xyz.mp4", "-f", "null", "-"])
    assert exc_info.value.code == "E101"


def test_run_success_with_progress_callback(wrapper, sample_video, tmp_path):
    output = tmp_path / "out.mp4"
    progresses = []
    wrapper.run(
        ["-y", "-i", str(sample_video), "-t", "1", "-c", "copy", str(output)],
        progress_callback=progresses.append,
    )
    assert output.exists()
    # 出力済みの映像フレーム数(25fps・1秒。コピーなので切れ目のパケットまで)
    assert progresses and progresses == sorted(progresses)
    assert 25 <= progresses[-1] <= 30


def test_failure_keeps_ffmpeg_message_when_progress_is_read(wrapper, tmp_path):
    # 進捗を読むと標準エラー出力が読み捨てられ、失敗の理由が消えていた
    missing = tmp_path / "missing_input.ts"
    with pytest.raises(MuxFailedError) as exc_info:
        wrapper.run(["-i", str(missing), "-f", "null", "-"], progress_callback=lambda frames: None)
    assert "missing_input.ts" in exc_info.value.message


def test_cancel_ends_running_ffmpeg(wrapper, tmp_path):
    # 中止ボタン: 実行中のffmpegを終了させ、E504になる。ファイルへ書き続ける
    # 長い処理(-re)で確かめる(すぐ終わる処理では止めたかどうか分からない)
    import threading
    import time

    from ts_smartcut.errors import ProcessingCancelledError

    output = tmp_path / "long.ts"
    timer = threading.Timer(1.0, wrapper.cancel_event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ProcessingCancelledError) as exc_info:
            wrapper.run(
                ["-y", "-re", "-f", "lavfi", "-i", "testsrc=duration=30:size=320x240:rate=25", str(output)],
                progress_callback=lambda frames: None,
            )
    finally:
        timer.cancel()
        wrapper.cancel_event.clear()
    assert exc_info.value.code == "E504"
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessingCancelledError):  # 中止の間は次の処理も始めない
        wrapper.cancel_event.set()
        try:
            wrapper.run(["-version"])
        finally:
            wrapper.cancel_event.clear()


# ---- get_source_timing / get_leading_picture_count (2026-10-04) --------------

def test_get_source_timing_detects_interlace_and_audio_offset(tmp_path):
    from tests._media_check import make_interlaced_source

    source = make_interlaced_source(
        tmp_path / "offset.ts", ["-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-g", "15"],
        "ac3", seconds=1, audio_lead=0.4,
    )
    timing = FFmpegWrapper().get_source_timing(source)

    assert timing.interlaced is True
    assert timing.field_order == "tt"
    assert timing.has_audio is True
    assert timing.audio_packet_duration == pytest.approx(0.032, abs=0.001)  # AC-3 48kHz
    # 音声が0.4秒先に始まる -> 映像の時刻はファイル先頭から約0.4秒後
    assert timing.seek_offset == pytest.approx(0.4, abs=0.05)


def test_get_source_timing_progressive_without_offset(sample_video):
    timing = FFmpegWrapper().get_source_timing(sample_video)
    assert timing.interlaced is False
    assert timing.seek_offset == pytest.approx(0.0, abs=0.001)


@pytest.mark.parametrize(
    "codec_args, expected",
    [
        # mpeg2videoはBフレームありで既定がオープンGOP: キーフレーム直前の2枚が後ろに格納される
        (["-c:v", "mpeg2video", "-bf", "2", "-g", "15"], 2),
        # クローズドGOPならリーディングピクチャは無い
        (["-c:v", "mpeg2video", "-bf", "2", "-g", "15", "-flags", "+cgop", "-sc_threshold", "1000000000"], 0),
    ],
)
def test_get_leading_picture_count(tmp_path, codec_args, expected):
    from tests._media_check import make_interlaced_source

    source = make_interlaced_source(tmp_path / "gop.ts", codec_args, "ac3", seconds=2)
    wrapper = FFmpegWrapper()
    fps = 30000 / 1001
    timing = wrapper.get_source_timing(source)
    keyframe_pts = timing.video_start + 30 / fps

    assert len(wrapper.get_leading_pictures(source, keyframe_pts, fps)) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="生バイトのファイル名はPOSIXのみで作れる")
def test_run_tolerates_non_utf8_ffmpeg_messages(tmp_path):
    # 放送TSでは番組情報(ARIB文字コード)がffmpegのメッセージにそのまま出る。
    # 以前はUTF-8として厳密に読んでUnicodeDecodeErrorで処理全体が落ちた
    # (実録画で確認)。ここでは読めないバイトを含むファイル名で再現する。
    missing = tmp_path / os.fsdecode(b"\xa4\xa2\xa4\xa4.ts")
    with pytest.raises(MuxFailedError):
        FFmpegWrapper().run(["-i", str(missing), "-f", "null", "-"])


def test_field_rate_reporting_is_not_vfr():
    # ソフトテレシネのMPEG-2: r_frame_rate=59.94(フィールド), avg=29.97(フレーム)
    assert _is_vfr_from_rates("30000/1001", "60000/1001") is False
    # 本当にばらつくVFR(スマホ動画等)は引き続きVFR
    assert _is_vfr_from_rates("2997/100", "30/1") is True


# ---- 走査方式の表示用判定 (get_scan_type) -------------------------------------------

def test_get_scan_type(wrapper, sample_video, tmp_path):
    from tests._media_check import make_interlaced_source, make_mixed_scan_source, make_soft_telecine_source
    from ts_smartcut.ffmpeg.wrapper import SCAN_INTERLACED, SCAN_MIXED, SCAN_PROGRESSIVE, SCAN_SOFT_TELECINE

    interlaced = make_interlaced_source(
        tmp_path / "i.ts", ["-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-b:v", "4M"], "ac3")
    assert wrapper.get_scan_type(sample_video) == SCAN_PROGRESSIVE
    assert wrapper.get_scan_type(interlaced) == SCAN_INTERLACED
    assert wrapper.get_scan_type(make_soft_telecine_source(tmp_path / "st.ts")) == SCAN_SOFT_TELECINE
    assert wrapper.get_scan_type(make_mixed_scan_source(tmp_path / "mixed.ts")) == SCAN_MIXED
