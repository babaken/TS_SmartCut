"""Rust で書いた部品 (native/ の ts_smartcut_native) のテスト。

2026-10-28 から特徴の計算の読み込みと自動補正の評価は Rust だけで行い、同じ処理の Python は削った
(autocut/native.py)。ここでは次を確かめる:

- モジュールの読み込み: 無い・版が合わない時は E506 (特徴の計算・自動補正は ffmpeg や記録を読む前に止まる)
- TS の解析 (StreamScanner): 正解は、削る前の Python の解析が同じ入力で出した結果 (壊れたデータは結果の要約)。
  テストの TS は手で組み立てるので毎回同じバイトになる。解析の決まりを意図して変えた時は、Rust の新しい結果を
  確かめてから正解を書き換える
- 特徴の計算: 値の中身は test_autocut_features.py・test_autocut_logo*.py が確かめる。ここでは同じ録画で毎回
  同じ結果になること、止める・失敗する時の動き、録画を ffmpeg に渡す所
- 読み終わった後の計算 (native/src/post.rs): 正解は 2026-10-28 まで使っていた numpy の計算 (テストの中に残した)。
  音量・コマとフレームの対応・float16 への丸め・ロゴの型が、ビットまで同じになること
- ロゴの型どうしの計算 (native/src/logo.rs、段階 ③-b): 正解は 2026-10-30 まで使っていた numpy の計算 (テストの
  中に残した)。局の型のまとめ方・型どうしの比べ方・材料からロゴを求める計算がビットまで同じになること。.lgd の型は
  背景のノイズの乱数が numpy と違うので、numpy の型と縁がほぼ合うこと
- ルールの案 (native/src/rules.rs): 正解は、削る前の Python の candidates.py・rules.py が合成の録画で出した案の
  要約 (区間・確信度・区間ごとの判定。理由の文は言語で変わるので除く)。決まりを意図して変えた時は、新しい案を
  確かめてから正解を書き換える
- 自動補正の評価: tuning.rs の組み立て (局の値をその録画を除いて測る・同じ録画をまとめる) と、録画を開いた時の
  案と同じ組み立て (proposals.py・rules.py を通す) が、同じ結果になること
"""

import hashlib
import json
import subprocess
import sys
import threading
import types

import numpy as np
import pytest

import ts_smartcut_native as native
from ts_smartcut.autocut import features
from ts_smartcut.autocut.features import KeepRange, compute_features
from ts_smartcut.autocut.logo import LogoTemplate
from ts_smartcut.autocut.native import NATIVE_VERSION, NativeModuleError, native_module
from ts_smartcut.autocut.stream_events import StreamEvents
from tests import test_stream_events as streams
from tests.test_autocut_features import _make_ts
from tests.test_autocut_logo import CM, FPS, TOTAL, logo_ts  # noqa: F401 (logo_ts はフィクスチャ)
from tests.test_autocut_logo_image import flat_logo_ts  # noqa: F401 (フィクスチャ)


# --- モジュールの読み込み ---------------------------------------------------------

def test_the_native_module_has_the_expected_version():
    assert native.VERSION == NATIVE_VERSION
    assert native_module() is native


def test_a_missing_module_is_e506(monkeypatch):
    monkeypatch.setitem(sys.modules, "ts_smartcut_native", None)  # import が ImportError になる
    with pytest.raises(NativeModuleError) as e:
        native_module()
    assert e.value.code == "E506"


@pytest.mark.parametrize("attribute,value", [("VERSION", NATIVE_VERSION + 1), ("THRESHOLD_NAMES", ["silence_db"])])
def test_a_module_that_does_not_match_is_e506(monkeypatch, attribute, value):
    monkeypatch.setattr(native, attribute, value)
    with pytest.raises(NativeModuleError) as e:
        native_module()
    assert e.value.code == "E506"


def test_features_and_tuning_stop_before_doing_anything_without_the_module(monkeypatch, tmp_path):
    from tests.test_autocut_thresholds import _two_quiet_recordings
    from ts_smartcut.autocut import tuning

    db = _two_quiet_recordings(tmp_path)
    monkeypatch.setitem(sys.modules, "ts_smartcut_native", None)
    started = []
    monkeypatch.setattr(features, "_probe", lambda *a, **k: started.append(1))
    with pytest.raises(NativeModuleError):
        compute_features(tmp_path / "x.ts", 29.97, [], temp_base=tmp_path / "temp")
    assert not started  # ffprobe・ffmpeg を起動していない
    monkeypatch.setattr(tuning, "load_samples", lambda *a, **k: started.append(2))
    with pytest.raises(NativeModuleError):
        tuning.tune_with_holdout(db, {}, "")
    assert not started  # 記録を読んでいない


# --- TS の解析 ---------------------------------------------------------------

def _scan(data: bytes, piece: int, service_id=streams.SERVICE_ID, video_start=None):
    """Rust の StreamScanner に piece バイトずつ渡した結果 (StreamEvents, 失敗したか)。"""
    scanner = native.StreamScanner(service_id, streams.VIDEO_START if video_start is None else video_start)
    failed = False
    for i in range(0, len(data), piece):
        failed |= scanner.feed_bytes(data[i:i + piece]) is not None
    return StreamEvents.from_native(*scanner.result(), complete=not failed), failed


def _digest(events: StreamEvents, failed: bool) -> str:
    """結果の要約 (削る前の Python の解析と同じ作り方)。"""
    value = [[[round(t, 6), [list(s) for s in layout]] for t, layout in events.pmt_changes],
             [[round(t, 6), c] for t, c in events.audio_channels],
             [round(float(x), 6) for x in events.caption_times], failed]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:16]


def _to_192(data: bytes) -> bytes:
    return b"".join(b"\x12\x34\x56\x78" + data[i:i + 188] for i in range(0, len(data), 188))


@pytest.fixture
def ts_data(tmp_path):
    return streams.make_ts(tmp_path / "streams.ts").read_bytes()


@pytest.mark.parametrize("piece", [1000, 188 * 7 + 5, 64 * 1024, 4 * 1024 * 1024])
def test_scanner_accepts_data_split_anywhere(ts_data, piece):
    events, failed = _scan(ts_data, piece)
    streams._check(events)
    assert not failed


def test_scanner_without_service_id_uses_the_first_program(ts_data):
    streams._check(_scan(ts_data, 64 * 1024, service_id=None)[0])


def test_scanner_reads_192_byte_packets(ts_data):
    streams._check(_scan(_to_192(ts_data), 5000)[0])


def test_scanner_finds_the_sync_again(ts_data):
    # 途中に半端なバイト(同期のずれ)・頭にごみ
    data = b"\x47\x00" * 300 + ts_data[:188 * 40 + 77] + ts_data[188 * 40:188 * 90] + b"\x00" * 13 + ts_data[188 * 90:]
    streams._check(_scan(data, 3000)[0])


def test_scanner_on_data_that_is_not_ts():
    events, failed = _scan(b"\x00" * 10000, 1000)
    assert events.pmt_changes == [] and events.audio_channels == [] and len(events.caption_times) == 0
    assert not failed


# 削る前の Python の解析が、下の壊れたデータ (種 0〜39) で出した結果の要約
DAMAGED_DIGESTS = [
    '54145590b1cf2eb2', '54145590b1cf2eb2', '54145590b1cf2eb2', '54145590b1cf2eb2',
    'bf19630e1c89bd57', 'b593a641b7b65193', '54145590b1cf2eb2', '54145590b1cf2eb2',
    '54145590b1cf2eb2', 'c08c721a77df68e6', 'd88917a558d72ead', '7c86c8d79fa3d77b',
    '54145590b1cf2eb2', '8439395cd33a5d85', '80e04f76f64731b7', '428f619667f07acc',
    '54145590b1cf2eb2', '0534d77fca08d728', '54145590b1cf2eb2', '54145590b1cf2eb2',
    '428f619667f07acc', '2ecd0973e85319b9', '6d2341433beab159', '428f619667f07acc',
    '54145590b1cf2eb2', '54145590b1cf2eb2', '89485b07afd179be', '54145590b1cf2eb2',
    '54145590b1cf2eb2', '9ccfad81dae15947', '54145590b1cf2eb2', '89485b07afd179be',
    '54145590b1cf2eb2', '351c6a84f60578d5', '54145590b1cf2eb2', '9ccfad81dae15947',
    '54145590b1cf2eb2', '54145590b1cf2eb2', 'ea6eca3ca0756c0f', '54145590b1cf2eb2',
]


@pytest.mark.parametrize("seed", range(40))
def test_scanner_on_damaged_data(ts_data, seed):
    """壊れたバイト・欠けたパケット(受信エラーの録画)でも、削る前の Python の解析と同じ結果・失敗の印。"""
    rng = np.random.default_rng(seed)
    data = bytearray(ts_data)
    for pos in rng.integers(0, len(data), 60):
        data[pos] = int(rng.integers(0, 256))
    for _ in range(5):
        cut = int(rng.integers(0, len(data) - 400))
        del data[cut:cut + int(rng.integers(1, 400))]
    assert _digest(*_scan(bytes(data), int(rng.integers(500, 20000)))) == DAMAGED_DIGESTS[seed]


# --- 特徴の計算 ----------------------------------------------------------------

def _compute(*args, **kwargs):
    ticks = []
    f = compute_features(*args, progress=ticks.append, **kwargs)
    return f, ticks


def _assert_same(a, b):
    for name in ("fps", "frame_count", "complete", "audio_rate", "audio_offset", "decoded_size", "corner_size",
                 "corner_kept_count", "corner_other_count", "logo_from_station"):
        assert getattr(a, name) == getattr(b, name), name
    for name in ("audio_level", "scene_change", "corner_kept_mean", "corner_kept_std", "corner_other_mean",
                 "corner_other_std", "logo_score"):
        np.testing.assert_array_equal(getattr(a, name), getattr(b, name), err_msg=name)
    for name in ("gx", "gy", "mask_x", "mask_y", "strength"):
        np.testing.assert_array_equal(getattr(a.logo_template, name), getattr(b.logo_template, name), err_msg=name)
    assert a.streams.to_meta() == b.streams.to_meta()
    np.testing.assert_array_equal(a.streams.caption_times, b.streams.caption_times)
    assert (a.logo_samples is None) == (b.logo_samples is None)
    if a.logo_samples is not None:
        assert a.logo_samples.to_blob() == b.logo_samples.to_blob()


@pytest.fixture(scope="module")
def mpeg2_ts(tmp_path_factory):
    return _make_ts(tmp_path_factory.mktemp("native") / "m.ts", "mpeg2video")


@pytest.fixture(scope="module")
def h264_ts(tmp_path_factory):
    return _make_ts(tmp_path_factory.mktemp("native") / "h.ts", "libx264")


@pytest.mark.parametrize("fixture", ["mpeg2_ts", "h264_ts"])
@pytest.mark.parametrize("keep", [[], [KeepRange(10, 70)], [KeepRange(0, 40), KeepRange(20, 30, excluded=True)]])
def test_features_are_the_same_every_time(fixture, keep, request):
    """同じ録画なら毎回同じ値・同じ進み具合 (スレッドの順で変わらない)。"""
    path = request.getfixturevalue(fixture)
    a, ticks_a = _compute(path, 29.97, keep)
    b, ticks_b = _compute(path, 29.97, keep)
    _assert_same(a, b)
    assert a.complete and ticks_a == ticks_b and ticks_a[-1] == a.frame_count


def test_logo_features_and_samples_are_the_same_every_time(logo_ts, flat_logo_ts):
    keep = [KeepRange(0, CM[0]), KeepRange(CM[1], TOTAL)]
    a, _ = _compute(logo_ts, FPS, keep)
    b, _ = _compute(logo_ts, FPS, keep)
    _assert_same(a, b)
    assert any(a.logo_template.has_logo)

    flat_a, _ = _compute(flat_logo_ts, FPS, keep)
    flat_b, _ = _compute(flat_logo_ts, FPS, keep)
    _assert_same(flat_a, flat_b)
    assert flat_a.logo_samples is not None

    station = LogoTemplate(a.logo_template.gx, a.logo_template.gy, a.logo_template.mask_x, a.logo_template.mask_y,
                           a.logo_template.strength, a.logo_template.frames)
    a2, _ = _compute(logo_ts, FPS, [], station_template=station)
    assert a2.logo_from_station and a2.logo_score.any()


def test_reservoir_keeps_the_same_frames_every_time(flat_logo_ts, monkeypatch):
    """MAX_PICTURES より多いキーフレームの時、置き換える位置は Python の固定の種の乱数で決まる。"""
    monkeypatch.setattr(features, "MAX_PICTURES", 3)
    keep = [KeepRange(0, CM[0]), KeepRange(CM[1], TOTAL)]
    a, _ = _compute(flat_logo_ts, FPS, keep)
    b, _ = _compute(flat_logo_ts, FPS, keep)
    _assert_same(a, b)
    assert a.logo_samples is not None and a.logo_samples.pictures > 3


def _image_reader_replaced_by(monkeypatch, code: str):
    """ロゴ画像の材料の ffmpeg を、code を動かす Python に置き換える。その標準エラーの中身を返すリスト。"""
    real_popen = subprocess.Popen

    def popen(cmd, *args, **kwargs):
        if "-skip_frame" in cmd:  # ロゴ画像の材料の ffmpeg (_LogoImageReader)
            cmd = [sys.executable, "-c", code]
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(features, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": popen}))
    stderr = []
    real_finish = features._LogoImageReader.finish

    def finish(self):
        ok = real_finish(self)
        stderr.append(b"".join(self._stderr))
        return ok

    monkeypatch.setattr(features._LogoImageReader, "finish", finish)
    return stderr


def test_the_logo_image_reader_gets_every_byte_of_the_recording(flat_logo_ts, monkeypatch):
    """ロゴ画像の材料の ffmpeg へも、本体の ffmpeg と同じ録画の全部のバイトを渡す。"""
    stderr = _image_reader_replaced_by(
        monkeypatch, "import sys; sys.stderr.write(str(len(sys.stdin.buffer.read())))")
    f, _ = _compute(flat_logo_ts, FPS, [KeepRange(0, CM[0]), KeepRange(CM[1], TOTAL)])
    assert f.complete and f.frame_count == TOTAL
    assert int(stderr[0]) == flat_logo_ts.stat().st_size


def test_features_are_computed_when_the_logo_image_reader_stops_early(flat_logo_ts, monkeypatch):
    """ロゴ画像の材料の ffmpeg が先に終わっても、本体の計算は最後まで続ける (材料だけ無い)。"""
    _image_reader_replaced_by(monkeypatch, "pass")
    f, _ = _compute(flat_logo_ts, FPS, [KeepRange(0, CM[0]), KeepRange(CM[1], TOTAL)])
    assert f.complete and f.frame_count == TOTAL and f.logo_samples is None


def test_stopping_returns_the_partial_result_and_leaves_no_threads(mpeg2_ts):
    stop = threading.Event()
    before = set(threading.enumerate())
    seen = []

    def progress(pictures):
        seen.append(pictures)
        stop.set()

    f = compute_features(mpeg2_ts, 29.97, [KeepRange(10, 70)], stop_event=stop, progress=progress)
    assert not f.complete and not f.streams.complete
    assert seen and f.frame_count <= seen[-1] + 1
    assert set(threading.enumerate()) <= before


def test_stopping_before_the_first_frame(mpeg2_ts):
    stop = threading.Event()
    stop.set()
    f = compute_features(mpeg2_ts, 29.97, [KeepRange(10, 70)], stop_event=stop)
    assert not f.complete and f.frame_count == 0


def test_an_error_in_the_progress_callback_stops_ffmpeg_and_is_raised(mpeg2_ts, tmp_path):
    def progress(_pictures):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        compute_features(mpeg2_ts, 29.97, [KeepRange(10, 70)], progress=progress, temp_base=tmp_path)
    # 一時フォルダ(run_<id>)は片付いている
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("run_")]


def test_a_missing_recording_is_not_complete(mpeg2_ts, monkeypatch, tmp_path):
    """読み始めた後に録画が読めなくなった時と同じく、そこまでで complete=False。"""
    calls = []
    real = native.FeatureReader

    def reader(file_path, *args, **kwargs):
        calls.append(file_path)
        return real(str(tmp_path / "gone.ts"), *args, **kwargs)

    monkeypatch.setattr(native, "FeatureReader", reader)
    f = compute_features(mpeg2_ts, 29.97, [])
    assert calls and not f.complete and f.frame_count == 0


def test_stopping_does_not_wait_for_the_next_frame(mpeg2_ts, monkeypatch):
    """ffmpeg の出力が途切れている間(PC が重い時)に止めても、次のフレームを待たずに ffmpeg を終わらせる。"""
    killed = threading.Event()
    real_kill = features.kill_process_tree

    def kill(process):
        killed.set()
        real_kill(process)

    monkeypatch.setattr(features, "kill_process_tree", kill)
    real = native.FeatureReader

    class Stalled:
        """フレームを読み始める前に、ffmpeg が終わらされるまで待つ(出力が途切れた状態)。"""

        def __init__(self, *args, **kwargs):
            self.reader = real(*args, **kwargs)

        def read_frames(self, tick, alive):
            assert killed.wait(10), "ffmpeg was not stopped while no frame arrived"
            return self.reader.read_frames(tick, alive)

        def finish(self):
            return self.reader.finish()

    monkeypatch.setattr(native, "FeatureReader", Stalled)
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    f = compute_features(mpeg2_ts, 29.97, [KeepRange(10, 70)], stop_event=stop)
    assert killed.is_set() and not f.complete


# --- 読み終わった後の計算 (native/src/post.rs) ---------------------------------------
# 正解は、2026-10-28 まで使っていた numpy の計算 (下の _numpy_*)。値がビットまで同じになること。
# (ロゴの一致度の重み付きの和だけは numpy が BLAS で足す順を決めないので比べない。test_autocut_logo.py が中身を見る)

import math  # noqa: E402

from ts_smartcut.autocut import logo  # noqa: E402


def _numpy_levels(peaks):
    p = np.maximum(peaks.astype(np.float64), 0.0)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(p / 32767.0)
    q = np.round((db - features.LEVEL_FLOOR_DB) * 2.0)
    return np.clip(np.nan_to_num(q, nan=0.0, neginf=0.0), 0, 255).astype(np.uint8)


def test_audio_levels_match_numpy_for_every_peak():
    peaks = np.arange(0, 32768, dtype=np.int16)
    np.testing.assert_array_equal(features.peaks_to_level(peaks), _numpy_levels(peaks))


def test_audio_peaks_match_numpy():
    from ts_smartcut.core.waveform import peaks_from_samples

    rng = np.random.default_rng(3)
    samples = rng.integers(-32768, 32768, 8000 * 3 + 37).astype(np.int16)
    samples[:80] = -32768  # 絶対値が 32768 になる所は 32767
    blocks = np.abs(samples[:len(samples) // 80 * 80].astype(np.int32)).reshape(-1, 80)
    np.testing.assert_array_equal(peaks_from_samples(samples), np.minimum(blocks.max(axis=1), 32767).astype(np.int16))


def _numpy_frames(pts, diffs, video_start, fps):
    keep = np.ones(len(pts), dtype=bool)
    keep[1:] = pts[1:] > np.maximum.accumulate(pts)[:-1]
    positions = np.nonzero(keep)[0]
    kept = pts[positions]
    count = max(int(math.floor((kept[-1] - video_start) * fps + 0.5)) + 1, 0)
    grid = video_start + (np.arange(count) - 0.5) / fps
    idx = np.minimum(np.searchsorted(kept, grid - 0.25 / 90000.0, side="left"), len(kept) - 1)
    first = np.ones(count, dtype=bool)
    first[1:] = idx[1:] != idx[:-1]
    idx = positions[idx]
    scene = np.where(first, diffs[idx], 0.0).astype(np.float16)
    scene[0] = 0.0
    return idx, scene, len(pts) - len(positions)


@pytest.mark.parametrize("seed", range(20))
def test_pictures_map_to_frames_like_numpy(seed):
    """ソフトテレシネ (2・3 フィールド)、時刻が戻るコマ (受信エラーの録画)、同じ時刻のコマを混ぜる。"""
    rng = np.random.default_rng(seed)
    fps = 30000 / 1001
    steps = rng.choice([1.0, 1.5, 1.0, 2.5], 3000) / fps
    pts = 1234.5 + np.cumsum(steps)
    for k in rng.integers(1, len(pts), 15):
        pts[k] = pts[k - 1] - rng.choice([0.0, 0.01, 1.0])
    diffs = (rng.random(len(pts)) * 80).astype(np.float32)
    idx, scene, skipped, _first = native.picture_frames(pts.tobytes(), diffs.tobytes(), 1234.5, fps)
    want_idx, want_scene, want_skipped = _numpy_frames(pts, diffs, 1234.5, fps)
    np.testing.assert_array_equal(np.frombuffer(idx, "<i8"), want_idx)
    assert np.frombuffer(scene, "<f2").tobytes() == want_scene.tobytes()
    assert skipped == want_skipped


def test_float16_rounding_matches_numpy():
    """四隅の平均と標準偏差 (float64 → float16)。丸めがちょうど半分になる値・小さい値・大きい値も入れる。"""
    rng = np.random.default_rng(5)
    n = 7
    total = np.concatenate([rng.random(5000) * 255 * n, (np.arange(2048) + 0.5) / 4 * n,
                            [0.0, 1e-9, 6.1e-5 * n, 65519.0 * n, 70000.0 * n]])
    square = total * total / n + rng.random(len(total)) * 100
    mean, std = native.corner_mean_std(total.tobytes(), square.tobytes(), n)
    want_mean = (total / n).astype(np.float16)
    want_std = np.sqrt(np.maximum(square / n - (total / n) ** 2, 0.0)).astype(np.float16)
    assert mean == want_mean.tobytes() and std == want_std.tobytes()


@pytest.mark.parametrize("seed", range(10))
def test_logo_template_matches_numpy(seed):
    rng = np.random.default_rng(seed)
    count = int(rng.integers(0, 300))
    sum_x = rng.integers(-count, count + 1, (4, 20, 29)).astype(np.int64)
    sum_y = rng.integers(-count, count + 1, (4, 19, 30)).astype(np.int64)
    t = logo.template_from_sums(sum_x.tobytes(), sum_y.tobytes(), (20, 30), count)
    n = max(count, 1)
    gx, gy = (sum_x / n).astype(np.float32), (sum_y / n).astype(np.float32)
    mask_x, mask_y = np.abs(gx) >= logo.CONSISTENCY, np.abs(gy) >= logo.CONSISTENCY
    if count == 0:
        mask_x[:], mask_y[:] = False, False
    assert t.gx.tobytes() == gx.tobytes() and t.gy.tobytes() == gy.tobytes()
    np.testing.assert_array_equal(t.mask_x, mask_x)
    np.testing.assert_array_equal(t.mask_y, mask_y)
    for i in range(4):
        selected = np.concatenate([np.abs(gx[i][mask_x[i]]), np.abs(gy[i][mask_y[i]])])
        assert t.strength[i] == (np.float32(selected.mean()) if len(selected) else np.float32(0))


# --- しきい値の自動補正の評価 (tuning.evaluate) ---------------------------------

from ts_smartcut.autocut import tuning  # noqa: E402
from ts_smartcut.autocut.candidates import find_boundaries  # noqa: E402
from ts_smartcut.autocut.evaluate import Score, compare  # noqa: E402
from ts_smartcut.autocut.proposals import item_measures, profile_from_measures  # noqa: E402
from ts_smartcut.autocut.rules import propose  # noqa: E402
from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, SPECS, consistency_error  # noqa: E402
from tests.test_autocut_rules import _features, _keep  # noqa: E402

TUNE_KEYS = ("4:211", "4:212", "6:300")


def _python_evaluate(samples, thresholds_for, score_ids=None) -> Score:
    """tuning.evaluate と同じ計算を、録画を開いた時の案に使う Python の部品で組み立てたもの
    (proposals.recording_proposals と同じく、局の値はその録画を除いた同じ局の録画から測る)。"""
    by_station = {}
    for s in samples:
        by_station.setdefault(s.station_key, []).append(s)
    total = Score()
    for key, group in by_station.items():
        t = thresholds_for(key)
        boundaries = {s.recording_id: find_boundaries(s.features, t) for s in group}
        measures = item_measures([(s.recording_id, s.features, s.truth, s.window) for s in group], t,
                                 boundaries_for=boundaries.__getitem__)
        for s in group:
            if score_ids is not None and s.recording_id not in score_ids:
                continue
            profile = profile_from_measures(measures, exclude_recording=s.recording_id)
            proposals = propose(s.features, s.window, profile, boundaries[s.recording_id], thresholds=t)
            total.add(compare(s.truth, proposals, s.features.frame_count, s.features.fps))
    return total


def _random_parts(rng):
    """番組の前後・本編・CM・提供・音の無い番宣を混ぜた、ばらばらの並び。"""
    parts = [("margin", float(rng.choice([3, 5, 12, 30])))]
    while len(parts) < 24:
        kind = rng.choice(["program", "cm", "cm", "cm", "credit", "silent"])
        if kind == "program":
            seconds = float(rng.uniform(20, 500))
        elif kind == "cm":
            seconds = float(rng.choice([5, 10, 14.5, 15, 15.5, 20, 30, 60, 90, 180, 181]))
        elif kind == "credit":
            seconds = float(rng.uniform(3, 12))
        else:
            seconds = float(rng.uniform(3, 43))
        parts.append((kind, seconds))
    parts.append(("margin", float(rng.choice([1.5, 10, 20]))))
    return parts


def _random_sample(seed, key):
    rng = np.random.default_rng(seed)
    parts = _random_parts(rng)
    f, spans = _features(parts, logo=bool(rng.random() < 0.6), seed=seed)
    # 書き換えるので numpy の配列にする (RecordingFeatures は numpy の配列も受け取る)
    f.audio_level, f.scene_change = np.asarray(f.audio_level), np.asarray(f.scene_change)
    f.logo_score = np.asarray(f.logo_score)
    # 長い無音(境目をいくつも探す)と、ロゴの弱い本編・ばらつく場面の切り替わり
    for kind, a, b in spans[1:]:
        if rng.random() < 0.2:
            s = int(a / f.fps * 100)
            f.audio_level[max(s - int(rng.uniform(150, 600)), 0):s] = int(rng.choice([0, 30, 60]))
            for k in rng.integers(max(a - 150, 1), a, 3):
                f.scene_change[k] = float(rng.uniform(10, 60))
        if kind == "program" and f.logo_template is not None and rng.random() < 0.3:
            f.logo_score[1, a:b] = int(rng.integers(20, 90))
    keep = [(a + int(rng.integers(-2, 3)), b - int(rng.integers(0, 4)), False) for a, b in _keep(spans)]
    if rng.random() < 0.3:
        a, b = sorted(int(x) for x in rng.integers(0, f.frame_count, 2))
        keep.append((a, b, True))
    window = None
    if rng.random() < 0.7:
        start = float(rng.uniform(0, parts[0][1] + 2))
        window = tuning.ProgramWindow(start, start + float(rng.uniform(600, f.frame_count / f.fps)))
    return tuning.TuneSample(1000 + seed, key, f, keep, window)


def _random_thresholds(rng):
    t = DEFAULT_THRESHOLDS
    for _ in range(int(rng.integers(0, 6))):
        spec = SPECS[int(rng.integers(len(SPECS)))]
        grid = spec.grid()
        candidate = t.replace(**{spec.name: grid[int(rng.integers(len(grid)))]})
        if consistency_error(candidate) is None:
            t = candidate
    return t


def _score_fields(score):
    return (float(score.truth_cut), float(score.proposed_cut), float(score.correct_cut), float(score.wrong_cut),
            dict(score.edges), score.extra_edges, score.recordings)


def test_the_threshold_names_match():
    assert tuple(native.THRESHOLD_NAMES) == tuple(s.name for s in SPECS)


@pytest.mark.parametrize("seed", range(12))
def test_evaluation_matches_the_proposal_code(seed):
    rng = np.random.default_rng(seed)
    samples = [_random_sample(seed * 10 + k, TUNE_KEYS[k % len(TUNE_KEYS)]) for k in range(7)]
    for trial in range(6):
        table = {key: _random_thresholds(rng) for key in TUNE_KEYS}
        score_ids = None
        if trial % 2:
            score_ids = {s.recording_id for s in samples if rng.random() < 0.4}
        python = _python_evaluate(samples, table.__getitem__, score_ids)
        rust = tuning.evaluate(samples, table.__getitem__, score_ids)
        assert _score_fields(rust) == _score_fields(python), (seed, trial)
        assert python.recordings > 0 or score_ids == set()


def _messy_truth(rng, frame_count):
    """重なる・はみ出す・隣り合う・空の区間を混ぜた正解(残す区間と除く区間)。"""
    truth = []
    for _ in range(int(rng.integers(0, 12))):
        a = int(rng.integers(-50, frame_count + 50))
        b = a + int(rng.choice([0, 1, int(rng.integers(-20, 2000))]))
        truth.append((a, b, bool(rng.random() < 0.25)))
    if truth and rng.random() < 0.5:
        a, b, _ = truth[0]
        truth.append((b, b + int(rng.integers(1, 300)), False))  # 隣り合う
    if rng.random() < 0.5:
        # 録画の先頭より前で終わる(Python の mask が末尾から数え直していた。2026-10-27)
        end = -int(rng.integers(1, 100))
        truth.append((end - int(rng.integers(0, 100)), end, bool(rng.random() < 0.3)))
    return truth


@pytest.mark.parametrize("seed", range(40))
def test_comparison_matches_the_proposal_code_on_messy_truth(seed):
    """compare は区間の重なりで数える(フレームごとの mask と同じ数になること)。"""
    rng = np.random.default_rng(700 + seed)
    samples = [_random_sample(700 + seed * 3 + k, "4:211") for k in range(3)]
    for s in samples:
        s.truth = _messy_truth(rng, s.features.frame_count)
    table = {"4:211": _random_thresholds(rng)}
    python = _python_evaluate(samples, table.__getitem__)
    rust = tuning.evaluate(samples, table.__getitem__)
    assert _score_fields(rust) == _score_fields(python)


def test_evaluation_of_an_empty_recording():
    f, _spans = _features([("program", 30)])
    sample = tuning.TuneSample(1, "4:211", f, [], None)
    f.frame_count = 0
    table = {"4:211": DEFAULT_THRESHOLDS}
    python = _python_evaluate([sample], table.__getitem__)
    rust = tuning.evaluate([sample], table.__getitem__)
    assert _score_fields(rust) == _score_fields(python)


# 削る前の Python のルールが、下の合成の録画 (種 0〜39) で出した案の要約
RULE_DIGESTS = [
    '1a6bcee4e5a8da6b', '1b0920246ee48649', '76108ccbe4b1d429', 'cb38fc3fbc7ab026',
    '6fc2eea6eb3f4fdb', 'b6bb0f75f1c5a38d', '520da63a3192f914', '27749df6d638a00b',
    'ca4ee4461ca857a4', '3549cd0c8298e8db', '3a482766275f074b', '558ccddf74b1ef57',
    '38e705226fd027d8', '2d48ade3fc3d1e37', '80b9c0368c821f7e', 'ba063e58c1724d7f',
    '9d2adf4acfca180b', '13f47053ddde8179', '23255fe26a24c9f6', '364d4d94304a1a88',
    'eb11fa121a87d049', '66b95cb4f3b17396', 'b4d7d6cfb7fa7fb6', '269e41221c47034c',
    'e3c4369fa1dcd064', '5397bba4e6d8d052', '2e1b27f50532d6fe', '1f24767760d6a71d',
    '5134d52814a4e649', 'ad56531a79dac1af', '1b8120d495908e25', 'fce1d1e58e5f817b',
    '14f391012e100414', 'a0b5bc09f413aacf', 'c82932840f35cab5', 'b84fde965dd2930f',
    '17f04f3033804815', '76cb5c5c9d516c0d', 'af1a322ff4292e7a', '16f5f8491474a0a2',
]


@pytest.mark.parametrize("seed", range(40))
def test_rules_give_the_same_proposals_as_the_old_python(seed):
    from ts_smartcut.autocut.rules import StationProfile, propose

    rng = np.random.default_rng(900 + seed)
    s = _random_sample(9000 + seed, "4:211")
    t = _random_thresholds(rng)
    profile = StationProfile(int(rng.integers(0, 4)), None if rng.random() < 0.3 else float(rng.random()))
    proposals = propose(s.features, s.window, profile, thresholds=t)
    text = repr([(p.start, p.end, p.confidence, [(d.segment.start, d.segment.end, d.kind, d.confidence,
                  None if d.logo is None else round(d.logo, 9), d.rule) for d in p.decisions]) for p in proposals])
    assert hashlib.sha256(text.encode()).hexdigest()[:16] == RULE_DIGESTS[seed]


# ---- 自動補正はスレッドで並べる ------------------------------------------------------

from tests.test_autocut_thresholds import _two_quiet_recordings  # noqa: E402


def test_tuning_threads_give_the_same_result_as_one_thread(tmp_path):
    db = _two_quiet_recordings(tmp_path, other_station=2)
    one = tuning.tune_with_holdout(db, {}, "", workers=1)
    threads = tuning.tune_with_holdout(db, {}, "", workers=3)
    assert one.folds == threads.folds == 2
    assert threads.changes == one.changes
    assert _score_fields(threads.after) == _score_fields(one.after)
    assert _score_fields(threads.holdout) == _score_fields(one.holdout)


def test_tuning_threads_stop_and_leave_no_threads(tmp_path):
    db = _two_quiet_recordings(tmp_path, other_station=2)
    stop = threading.Event()

    def progress(done, _total):
        if done >= 1:
            stop.set()

    with pytest.raises(tuning.TuningCancelled):
        tuning.tune_with_holdout(db, {}, "", stop_event=stop, progress=progress, workers=2)
    assert not [t for t in threading.enumerate() if t.name.startswith("autocut-tuning")]


def test_tuning_threads_use_half_the_cpus(monkeypatch):
    monkeypatch.setattr(tuning.os, "process_cpu_count", lambda: 16, raising=False)
    assert tuning.worker_count(23) == 8  # 論理 CPU の半分
    assert tuning.worker_count(3) == 3  # 仕事の数まで
    monkeypatch.setattr(tuning.os, "process_cpu_count", lambda: 1, raising=False)
    assert tuning.worker_count(23) == 1


# ---- ロゴの型どうしの計算 (native/src/logo.rs、段階 ③-b) ---------------------------------
# 正解は 2026-10-30 まで logo.py・logo_image.py・lgd_import.py で使っていた numpy の計算

from ts_smartcut.autocut import lgd_import, logo_image  # noqa: E402
from ts_smartcut.autocut.logo_image import CornerSamples  # noqa: E402
from ts_smartcut.autocut.packed import Packed  # noqa: E402


def _numpy_template(t):
    """型の値を numpy の配列にしたもの (numpy の計算の正解用)。"""
    return types.SimpleNamespace(gx=np.asarray(t.gx), gy=np.asarray(t.gy), mask_x=np.asarray(t.mask_x),
                                 mask_y=np.asarray(t.mask_y), frames=t.frames, has_logo=np.asarray(t.has_logo))


def _numpy_combine(usable):
    usable = [_numpy_template(t) for t in usable]
    frames = sum(t.frames for t in usable)
    gx = sum(t.gx.astype(np.float64) * t.frames for t in usable) / frames
    gy = sum(t.gy.astype(np.float64) * t.frames for t in usable) / frames
    gx, gy = gx.astype(np.float32), gy.astype(np.float32)
    mask_x, mask_y = np.abs(gx) >= logo.CONSISTENCY, np.abs(gy) >= logo.CONSISTENCY
    needed = math.ceil(len(usable) * logo.STATION_AGREEMENT - 1e-9)
    mask_x &= sum((np.abs(t.gx) >= logo.CONSISTENCY).astype(np.int32) for t in usable) >= needed
    mask_y &= sum((np.abs(t.gy) >= logo.CONSISTENCY).astype(np.int32) for t in usable) >= needed
    strength = np.zeros(4, np.float32)
    for i in range(4):
        selected = np.concatenate([np.abs(gx[i][mask_x[i]]), np.abs(gy[i][mask_y[i]])])
        strength[i] = float(selected.mean()) if len(selected) else 0.0
    return gx, gy, mask_x, mask_y, strength


def _random_template(rng, h=12, w=18, frames=None):
    """割合が -1〜1 の型。ロゴのある隅と弱い隅、割合がちょうど 0 の画素を混ぜる。"""
    frames = int(rng.integers(1, 500)) if frames is None else frames
    sum_x = rng.integers(-frames, frames + 1, (4, h, w - 1))
    sum_y = rng.integers(-frames, frames + 1, (4, h - 1, w))
    quiet = rng.random(4) < 0.4
    sum_x[quiet] //= 5
    sum_y[quiet] //= 5
    sum_x[rng.random(sum_x.shape) < 0.1] = 0
    return logo.template_from_sums(sum_x.astype(np.int64).tobytes(), sum_y.astype(np.int64).tobytes(), (h, w), frames)


@pytest.mark.parametrize("seed", range(15))
def test_station_template_matches_numpy(seed):
    rng = np.random.default_rng(700 + seed)
    usable = [_random_template(rng) for _ in range(int(rng.integers(2, 7)))]
    station = logo.combine_templates(usable)
    gx, gy, mask_x, mask_y, strength = _numpy_combine(usable)
    assert station.gx.tobytes() == gx.tobytes() and station.gy.tobytes() == gy.tobytes()
    np.testing.assert_array_equal(np.asarray(station.mask_x), mask_x)
    np.testing.assert_array_equal(np.asarray(station.mask_y), mask_y)
    assert np.asarray(station.strength, np.float32).tobytes() == strength.tobytes()
    assert station.frames == sum(t.frames for t in usable)


def _numpy_agreement(imported, reference):
    imported, reference = _numpy_template(imported), _numpy_template(reference)
    if not reference.has_logo.any():
        return None
    votes = []
    for i in range(4):
        if not imported.has_logo[i]:
            continue
        if not reference.has_logo[i]:
            votes.append(np.zeros(int(imported.mask_x[i].sum() + imported.mask_y[i].sum()), np.float32))
            continue
        mx, my = imported.mask_x[i], imported.mask_y[i]
        votes.append(np.concatenate([np.sign(imported.gx[i][mx]) * reference.gx[i][mx],
                                     np.sign(imported.gy[i][my]) * reference.gy[i][my]]))
    values = np.concatenate(votes) if votes else np.zeros(0, np.float32)
    return float((values >= logo.CONSISTENCY).mean()) if len(values) else None


@pytest.mark.parametrize("seed", range(15))
def test_logo_agreement_matches_numpy(seed):
    rng = np.random.default_rng(800 + seed)
    a, b = _random_template(rng), _random_template(rng)
    if seed % 5 == 0:
        b.mask_x = Packed.zeros(b.mask_x.shape, "b1")  # 比べる型にロゴが無い
        b.mask_y = Packed.zeros(b.mask_y.shape, "b1")
    for x, y in ((a, b), (b, a), (a, a)):
        assert logo.logo_agreement(x, y) == _numpy_agreement(x, y)
    values = [v for v in (_numpy_agreement(a, b), _numpy_agreement(b, a)) if v is not None]
    assert logo.two_way_agreement(a, b) == (max(values) if values else None)
    assert logo.logo_agreement(a, _random_template(rng, h=10)) is None  # 大きさが違う


def _numpy_solve(samples):
    x0 = min(s.box[0] for s in samples)
    y0 = min(s.box[1] for s in samples)
    x1 = max(s.box[0] + s.box[2] for s in samples)
    y1 = max(s.box[1] + s.box[3] for s in samples)
    alpha, color, frames = [], [], []
    for p in range(3):
        d = 1 if p == 0 else 2
        shape = ((y1 - y0) // d, (x1 - x0) // d)
        n, sb, sbb, sv, sbv = (np.zeros(shape) for _ in range(5))
        for s in samples:
            x, y, w, h = s.box
            sl = (slice((y - y0) // d, (y - y0 + h) // d), slice((x - x0) // d, (x - x0 + w) // d))
            n[sl] += s.n
            sb[sl] += s.sum_b[p]
            sbb[sl] += s.sum_bb[p]
            sv[sl] += s.sum_v[p]
            sbv[sl] += s.sum_bv[p]
        den = n * sbb - sb * sb
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = np.where(den > 0, (n * sbv - sb * sv) / den, 1.0)
            intercept = np.where(n > 0, (sv - slope * sb) / n, 0.0)
            a = np.clip(1.0 - slope, 0.0, 1.0)
            c = np.where(a > 0.02, intercept / np.maximum(a, 1e-9), 0.0)
        alpha.append(a)
        color.append(np.clip(c, 0.0, 255.0))
        frames.append(n)
    return (x0, y0, x1 - x0, y1 - y0), alpha, color, frames


def _random_samples(rng):
    """背景 b に不透明度 a・色 c のロゴを重ねたフレームの合計 (録画ごとに範囲をずらす。1 フレームだけの録画も)。"""
    out = []
    for _ in range(int(rng.integers(1, 5))):
        x, y = 2 * int(rng.integers(500, 520)), 2 * int(rng.integers(10, 30))
        w, h = 2 * int(rng.integers(10, 40)), 2 * int(rng.integers(8, 20))
        n = int(rng.choice([1, int(rng.integers(2, 200))]))
        backgrounds = rng.uniform(16, 235, (n, 3))
        sum_v, sum_bv = [], []
        for p in range(3):
            shape = (h, w) if p == 0 else (h // 2, w // 2)
            a = rng.uniform(0, 1, shape) * (rng.random(shape) < 0.5)
            c = rng.uniform(16, 235, shape)
            seen = (1 - a) * backgrounds[:, p, None, None] + a * c + rng.normal(0, 1, (n,) + shape)
            sum_v.append(seen.sum(axis=0))
            sum_bv.append((backgrounds[:, p, None, None] * seen).sum(axis=0))
        out.append(CornerSamples(1, (x, y, w, h), n, backgrounds.sum(axis=0), (backgrounds ** 2).sum(axis=0),
                                 sum_v, sum_bv))
    return out


@pytest.mark.parametrize("seed", range(15))
def test_solve_matches_numpy(seed):
    samples = _random_samples(np.random.default_rng(900 + seed))
    solved = logo_image.solve(samples)
    box, alpha, color, frames = _numpy_solve(samples)
    assert solved.box == box
    for p in range(3):
        assert solved.alpha[p].tobytes() == alpha[p].tobytes()
        assert solved.color[p].tobytes() == color[p].tobytes()
        assert solved.frames[p].tobytes() == frames[p].tobytes()
    assert logo_image.solve([]) is None


def _numpy_sample_checks(samples, area, min_frames, logo_alpha, dark_alpha, dark_luma, max_dark, min_corr):
    """lgd_export の修正 5 (logo_sample_checks) の numpy 版: 録画ごとに求めて局の範囲で比べる。"""
    ax, ay, aw, ah = area

    def corr(a, b):
        ok = ~np.isnan(a) & ~np.isnan(b)
        if ok.sum() < 3 or a[ok].std() == 0 or b[ok].std() == 0:
            return -1.0
        return float(np.corrcoef(a[ok], b[ok])[0, 1])

    checks, alphas, judged = [], [], []
    for i, s in enumerate(samples):
        if s.n < min_frames:
            checks.append(1)
            continue
        (sx, sy, sw, sh), alpha, color, _frames = _numpy_solve([s])
        a, c = np.full((ah, aw), np.nan), np.full((ah, aw), np.nan)
        x0, y0, x1, y1 = max(sx, ax), max(sy, ay), min(sx + sw, ax + aw), min(sy + sh, ay + ah)
        if x1 > x0 and y1 > y0:
            a[y0 - ay:y1 - ay, x0 - ax:x1 - ax] = alpha[0][y0 - sy:y1 - sy, x0 - sx:x1 - sx]
            c[y0 - ay:y1 - ay, x0 - ax:x1 - ax] = color[0][y0 - sy:y1 - sy, x0 - sx:x1 - sx]
        with np.errstate(invalid="ignore"):
            logo = a > logo_alpha
            dark = logo & (a >= dark_alpha) & (c < dark_luma)
        if logo.sum() > 0 and dark.sum() >= max_dark * logo.sum():
            checks.append(2)
            continue
        checks.append(0)
        alphas.append(a)
        judged.append(i)
    if len(judged) == 2 and corr(*alphas) < min_corr:
        checks[judged[0]] = checks[judged[1]] = 3
    elif len(judged) >= 3:
        with np.errstate(all="ignore"):
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                median = np.nanmedian(np.array(alphas), axis=0)
        for a, i in zip(alphas, judged):
            if corr(a, median) < min_corr:
                checks[i] = 3
    return checks


def _check_samples(rng, area):
    """同じロゴの録画に、ロゴがずれた録画・暗い帯の録画・フレームの少ない録画を混ぜる (範囲は録画ごとに広げる)。"""
    ax, ay, aw, ah = area
    logo_a = np.zeros((ah, aw))
    logo_a[ah // 4:3 * ah // 4, aw // 4:3 * aw // 4] = 0.3
    out = []
    for _ in range(int(rng.integers(2, 7))):
        grow = 2 * int(rng.integers(0, 3))
        x, y, w, h = ax - grow, ay - grow, aw + 2 * grow, ah + 2 * grow
        kind = rng.choice(["good", "good", "shifted", "dark"])
        n = int(rng.choice([int(rng.integers(40, 200)), int(rng.integers(5, 30))], p=[0.85, 0.15]))
        backgrounds = rng.uniform(16, 235, (n, 3))
        sum_v, sum_bv = [], []
        for p in range(3):
            d = 1 if p == 0 else 2
            a = np.zeros((h // d, w // d))
            if kind == "dark":
                a[:] = 0.95
                c = 20.0
            else:
                core = logo_a[::d, ::d]
                if kind == "shifted":
                    core = np.roll(core, (ah // (3 * d), aw // (3 * d)), axis=(0, 1))
                a[grow // d:grow // d + core.shape[0], grow // d:grow // d + core.shape[1]] = core
                c = 220.0
            seen = (1 - a) * backgrounds[:, p, None, None] + a * c + rng.normal(0, 1, (n,) + a.shape)
            sum_v.append(seen.sum(axis=0))
            sum_bv.append((backgrounds[:, p, None, None] * seen).sum(axis=0))
        out.append(CornerSamples(1, (x, y, w, h), n, backgrounds.sum(axis=0), (backgrounds ** 2).sum(axis=0),
                                 sum_v, sum_bv))
    return out


@pytest.mark.parametrize("seed", range(20))
def test_sample_checks_match_numpy(seed):
    from ts_smartcut.autocut import lgd_export

    area = (1000, 20, 64, 32)
    samples = _check_samples(np.random.default_rng(1300 + seed), area)
    args = (lgd_export.MIN_PIXEL_FRAMES, lgd_export.LOGO_ALPHA, lgd_export.DARK_ALPHA, lgd_export.DARK_LUMA,
            lgd_export.MAX_DARK_FRACTION, lgd_export.MIN_AGREEMENT)
    assert lgd_export.sample_checks(samples, area) == _numpy_sample_checks(samples, area, *args)


def _numpy_lgd_template(lgd):
    """2026-10-30 までの lgd_import.template_from_lgd (背景のノイズは numpy の乱数)。"""
    width, height = lgd.image_width, lgd.image_height
    w, h = -(-width // 4), -(-height // 4)
    cw, ch = w // 4, h // 4
    full_w, full_h = cw * 4, ch * 4
    origins = [(0, 0), (0, (w - cw) * 4), ((h - ch) * 4, 0), ((h - ch) * 4, (w - cw) * 4)]
    rng = np.random.default_rng(0)
    levels = np.linspace(16.0, 235.0, 64).astype(np.float32)
    corners = np.broadcast_to(levels[:, None, None, None], (64, 4, ch, cw)).copy()
    for i, (oy, ox) in enumerate(origins):
        y0, y1 = max(lgd.y, oy), min(lgd.y + lgd.height, oy + full_h)
        x0, x1 = max(lgd.x, ox), min(lgd.x + lgd.width, ox + full_w)
        if y0 >= y1 or x0 >= x1:
            continue
        image = levels[:, None, None] + rng.normal(0.0, 3.0, (64, full_h, full_w)).astype(np.float32)
        a = np.asarray(lgd.alpha)[y0 - lgd.y:y1 - lgd.y, x0 - lgd.x:x1 - lgd.x]
        c = np.asarray(lgd.luma)[y0 - lgd.y:y1 - lgd.y, x0 - lgd.x:x1 - lgd.x]
        region = image[:, y0 - oy:y1 - oy, x0 - ox:x1 - ox]
        image[:, y0 - oy:y1 - oy, x0 - ox:x1 - ox] = region * (1.0 - a) + c * a
        small = image.reshape(64, ch, 4, cw, 4).mean(axis=(2, 4))
        corners[:, i] = np.clip(np.round(small), 0, 255)
    return logo.template_from_corners(corners)


@pytest.mark.parametrize("size, box, value", [
    ((1280, 720), (1080, 40, 140, 70), 500),  # 右上の半透明の枠
    ((1920, 1080), (60, 50, 200, 90), 800),  # 左上
    ((1440, 1080), (1200, 960, 180, 80), 300),  # 右下の薄い枠
])
def test_lgd_template_agrees_with_the_numpy_one(tmp_path, size, box, value):
    from tests.test_autocut_lgd_import import _ring_alpha, write_lgd

    x, y, w, h = box
    path = write_lgd(tmp_path / "t.lgd", x, y, _ring_alpha(w, h, 10, value), image_size=size)
    lgd = lgd_import.read_lgd(path)[0]
    rust, old = lgd_import.template_from_lgd(lgd), _numpy_lgd_template(lgd)
    assert rust.gx.shape == old.gx.shape and rust.frames == old.frames == 64
    np.testing.assert_array_equal(rust.has_logo, old.has_logo)
    assert logo.two_way_agreement(rust, old) >= 0.95
    for i in np.nonzero(old.has_logo)[0]:  # マスクの画素の数もほぼ同じ
        old_pixels = old.mask_x[i].count_nonzero() + old.mask_y[i].count_nonzero()
        rust_pixels = rust.mask_x[i].count_nonzero() + rust.mask_y[i].count_nonzero()
        assert abs(rust_pixels - old_pixels) <= max(2, old_pixels // 20)


def test_lgd_template_is_the_same_every_time(tmp_path):
    from tests.test_autocut_lgd_import import _box_lgd

    lgd = lgd_import.read_lgd(_box_lgd(tmp_path / "t.lgd"))[0]
    a, b = lgd_import.template_from_lgd(lgd), lgd_import.template_from_lgd(lgd)
    assert a.gx.tobytes() == b.gx.tobytes() and a.gy.tobytes() == b.gy.tobytes()
