"""時系列の特徴の計算 (自動カット計画 A3)。

場面の切り替わりと無音の位置が分かっている合成の TS を ffmpeg で作り、
特徴の位置がフレーム単位で合うことを確かめる(モックしない)。
"""

import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

from ts_smartcut.autocut.feature_worker import FeatureJob, FeatureWorker
from ts_smartcut.autocut.features import (
    FEATURE_VERSION,
    KeepRange,
    RecordingFeatures,
    compute_features,
    increasing_pictures,
    map_pictures_to_frames,
    peaks_to_level,
)
from ts_smartcut.autocut.history_db import CURRENT_SCHEMA_VERSION, CutHistoryDB

FPS = 30000 / 1001
TOTAL = 300  # フレーム数(約10秒)
SCENES = (90, 200)  # 場面が切り替わるフレーム
SILENCE = (4.0, 5.0)  # 無音の時間帯(映像の先頭から、秒)
LEVELS = (60, 190, 110)  # 場面ごとの明るさ


def _make_ts(path: Path, vcodec: str) -> Path:
    s1, s2 = SCENES
    lum = f"if(lt(N,{s1}),{LEVELS[0]},if(lt(N,{s2}),{LEVELS[1]},{LEVELS[2]}))"
    a0, a1 = SILENCE
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=gray:s=320x240:r=30000/1001,format=yuv420p,geq=lum='{lum}':cb=128:cr=128",
            "-f", "lavfi", "-i", f"aevalsrc='if(between(t,{a0},{a1}),0,0.5*sin(2*PI*440*t))':s=48000",
            "-frames:v", str(TOTAL), "-t", f"{TOTAL / FPS:.3f}",
            "-c:v", vcodec, "-q:v" if vcodec == "mpeg2video" else "-crf", "4" if vcodec == "mpeg2video" else "18",
            "-g", "15", "-bf", "2",
            "-c:a", "aac", "-b:a", "128k", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def mpeg2_ts(tmp_path_factory):
    return _make_ts(tmp_path_factory.mktemp("features") / "scenes_mpeg2.ts", "mpeg2video")


@pytest.fixture(scope="module")
def h264_ts(tmp_path_factory):
    return _make_ts(tmp_path_factory.mktemp("features") / "scenes_h264.ts", "libx264")


@pytest.mark.parametrize("fixture", ["mpeg2_ts", "h264_ts"])
def test_scene_changes_and_silence_are_found_at_the_right_frames(fixture, request):
    path = request.getfixturevalue(fixture)
    f = compute_features(path, FPS, [KeepRange(0, SCENES[0])])

    assert f.complete
    assert f.frame_count == TOTAL
    scene = f.scene_change.astype(np.float32)
    # 切り替わりのフレームだけが大きく、他はほぼ 0
    top = sorted(np.argsort(scene)[-2:].tolist())
    assert top == list(SCENES)
    others = np.delete(scene, list(SCENES))
    assert others.max() < 3.0
    assert scene[SCENES[0]] > 100 and scene[SCENES[1]] > 50

    # 音量: 無音の時間帯の内側は -60dB 未満、外側は -15dB より大きい
    db = np.asarray(f.audio_db())

    def at(seconds):
        return db[int(np.floor((seconds - f.audio_offset) * f.audio_rate))]

    assert all(at(t) < -60 for t in np.arange(SILENCE[0] + 0.1, SILENCE[1] - 0.1, 0.05))
    assert all(at(t) > -15 for t in np.arange(1.0, SILENCE[0] - 0.1, 0.25))
    assert all(at(t) > -15 for t in np.arange(SILENCE[1] + 0.1, 9.0, 0.25))


def test_corner_statistics_are_split_by_kept_ranges(mpeg2_ts):
    s1, s2 = SCENES
    f = compute_features(mpeg2_ts, FPS, [KeepRange(0, s1), KeepRange(s2, TOTAL, excluded=True)])

    assert f.corner_kept_count == s1
    assert f.corner_other_count == s2 - s1  # 基準外の区間はどちらにも入れない
    w, h = f.decoded_size
    assert (w, h) == (80, 60)  # 320x240 の 1/4
    assert f.corner_size == (20, 15)
    assert f.corner_kept_mean.shape == (4, 15, 20)
    # 灰色の画面の明るさ(format=gray は 16〜235 の Y を 0〜255 に広げる)。残した区間は1つ目の場面だけ
    assert np.allclose(f.corner_kept_mean.astype(np.float32), _full_range(LEVELS[0]), atol=3)
    assert np.asarray(f.corner_kept_std, np.float32).max() < 2
    assert np.allclose(f.corner_other_mean.astype(np.float32), _full_range(LEVELS[1]), atol=3)


def _full_range(y):
    return (y - 16) * 255 / 219


def test_blob_round_trip(mpeg2_ts):
    f = compute_features(mpeg2_ts, FPS, [KeepRange(0, 100)])
    g = RecordingFeatures.from_blob(f.to_blob())
    assert g.version == FEATURE_VERSION
    assert (g.frame_count, g.complete, g.audio_rate, g.decoded_size, g.corner_size) == (
        f.frame_count, f.complete, f.audio_rate, f.decoded_size, f.corner_size,
    )
    assert g.audio_offset == pytest.approx(f.audio_offset)
    for name in ("audio_level", "scene_change", "corner_kept_mean", "corner_kept_std",
                 "corner_other_mean", "corner_other_std"):
        assert np.array_equal(getattr(g, name), getattr(f, name)), name


def test_stopping_keeps_what_was_computed(mpeg2_ts):
    stop = threading.Event()
    f = compute_features(mpeg2_ts, FPS, [], stop_event=stop, progress=lambda n: stop.set())
    assert not f.complete
    assert 0 < f.frame_count < TOTAL


def test_temp_files_go_to_the_app_temp_dir_and_are_removed(mpeg2_ts, tmp_path):
    """一時ファイルはアプリの一時フォルダ(既定は実行ファイルの直下の Temp/)の run_<id>/ に
    作り、終わったら消す。異常終了で残った run_ フォルダは次の計算の始めに消す。"""
    base = tmp_path / "Temp"
    stale = base / "run_stale"
    stale.mkdir(parents=True)
    (stale / "audio.s16").write_bytes(b"x" * 100)
    seen = []
    f = compute_features(mpeg2_ts, FPS, [], temp_base=base,
                         progress=lambda n: seen.extend(p.name for p in base.iterdir()))
    assert f.complete
    assert seen and all(name.startswith("run_") and name != "run_stale" for name in seen)
    assert list(base.iterdir()) == []


def test_default_temp_dir_is_under_the_executable(monkeypatch, tmp_path):
    import sys

    from ts_smartcut.system import temp_file_manager

    exe = tmp_path / "app" / "TS_SmartCut.exe"
    exe.parent.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert temp_file_manager.app_temp_dir() == exe.parent / "Temp"
    assert (exe.parent / "Temp").is_dir()


def test_missing_video_raises(tmp_path):
    path = tmp_path / "audio_only.ts"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", str(path)], check=True,
    )
    from ts_smartcut.errors import FFmpegError

    with pytest.raises(FFmpegError):
        compute_features(path, FPS, [])


def test_soft_telecine_pictures_map_to_the_cut_engine_frame_numbers():
    # 24p のコマを 29.97 の刻みで 3,2,3,2 フィールドずつ表示(コマの長さ 1.5, 1, 1.5, 1 フレーム)
    start = 100.0
    pictures = np.array([0.0, 1.5, 2.5, 4.0, 5.0]) / FPS + start
    idx, first = map_pictures_to_frames(pictures, start, FPS)
    # フレーム N は 表示時刻 >= start + (N-0.5)/fps の最初のコマ
    assert idx.tolist() == [0, 1, 1, 2, 3, 4]
    assert first.tolist() == [True, True, False, True, True, True]


def test_pictures_before_the_first_decodable_one_map_to_it():
    # 録画の先頭のコマがデコードできない(BS-A は 17 コマ)時、先頭のフレームは最初のコマ
    start = 10.0
    pictures = (np.arange(3, 8) / FPS) + start
    idx, first = map_pictures_to_frames(pictures, start, FPS)
    assert len(idx) == 8
    assert idx[:4].tolist() == [0, 0, 0, 0]
    assert first[:4].tolist() == [True, False, False, False]


def test_peaks_to_level():
    level = peaks_to_level(np.array([0, 32767, 3277, 1], dtype=np.int16))
    db = np.asarray(level, np.float32) / 2 - 100
    assert db[0] == -100  # 無音
    assert db[1] == 0
    assert db[2] == pytest.approx(-20, abs=0.5)
    assert db[3] == pytest.approx(-90.3, abs=0.5)


# ---- DB と裏のスレッド -----------------------------------------------------

def _db_with_recordings(tmp_path, count):
    """features は recordings を参照するので、録画の行を作っておく(id は 1 から)。"""
    db = CutHistoryDB(tmp_path / "cut_history.db")
    with db.get_connection() as conn:
        for i in range(count):
            db.insert_recording(conn, {"file_name": f"r{i}.ts", "file_size": i, "file_hash": str(i), "created_at": "now"})
    return db


def test_worker_computes_and_saves_features(tmp_path, mpeg2_ts):
    db = _db_with_recordings(tmp_path, 1)
    worker = FeatureWorker(db)
    worker.submit([FeatureJob(recording_id=1, file_path=mpeg2_ts, fps=FPS, keep=[KeepRange(0, 90)])])
    assert worker.wait_idle(60)

    row = db.get_features_row(1)
    assert row is not None
    assert (row["feature_version"], row["is_complete"], row["frame_count"]) == (FEATURE_VERSION, 1, TOTAL)
    f = RecordingFeatures.from_blob(row["data"])
    assert f.corner_kept_count == 90
    # しきい値の自動補正の対象の局を決める(最後まで計算した録画だけ。取り出すと空になる)
    assert worker.take_saved_recordings() == [1]
    assert worker.take_saved_recordings() == []


def test_worker_skips_missing_files_and_continues(tmp_path, mpeg2_ts):
    db = _db_with_recordings(tmp_path, 2)
    worker = FeatureWorker(db)
    worker.submit([
        FeatureJob(recording_id=1, file_path=tmp_path / "deleted.ts", fps=FPS),
        FeatureJob(recording_id=2, file_path=mpeg2_ts, fps=FPS),
    ])
    assert worker.wait_idle(60)
    assert db.get_features_row(1) is None
    assert db.get_features_row(2) is not None


def test_worker_stop_saves_the_partial_result(tmp_path, mpeg2_ts):
    db = _db_with_recordings(tmp_path, 3)
    started = threading.Event()

    def slow_compute(path, fps, keep, stop_event, progress=None, **kwargs):
        def slow_progress(_n):
            started.set()
            stop_event.wait(10)  # アプリの終了(stop)まで待つ
        return compute_features(path, fps, keep, stop_event=stop_event, progress=slow_progress, **kwargs)

    worker = FeatureWorker(db, compute=slow_compute)
    worker.submit([
        FeatureJob(recording_id=1, file_path=mpeg2_ts, fps=FPS),
        FeatureJob(recording_id=2, file_path=mpeg2_ts, fps=FPS),
    ])
    assert started.wait(30)
    worker.stop(timeout=30)

    row = db.get_features_row(1)
    assert row is not None and row["is_complete"] == 0
    assert 0 < row["frame_count"] < TOTAL
    assert db.get_features_row(2) is None  # 始めていない録画は計算しない
    assert worker.take_saved_recordings() == []  # 途中までの特徴は補正に使わない
    assert not worker.is_busy
    worker.submit([FeatureJob(recording_id=3, file_path=mpeg2_ts, fps=FPS)])  # 終了後は受け付けない
    assert db.get_features_row(3) is None


def test_database_v1_is_migrated_with_feature_columns(tmp_path):
    import sqlite3

    path = tmp_path / "cut_history.db"
    db = CutHistoryDB(path)
    with db.get_connection() as conn:
        # A2 の時点(v1)の features 表に戻す
        conn.execute("DROP TABLE features")
        conn.execute(
            "CREATE TABLE features (recording_id INTEGER PRIMARY KEY, computed_at TEXT NOT NULL, data BLOB NOT NULL)"
        )
        conn.execute("INSERT INTO features VALUES (5, 'old', x'00')")
        conn.execute("DELETE FROM schema_info WHERE version > 1")

    CutHistoryDB(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0] == CURRENT_SCHEMA_VERSION
        columns = {row[1] for row in conn.execute("PRAGMA table_info(features)")}
        assert {"feature_version", "is_complete", "frame_count"} <= columns
        # 古い行は「版 0・途中まで」として残る(計算し直しの対象)
        assert conn.execute("SELECT feature_version, is_complete FROM features WHERE recording_id = 5").fetchone() == (0, 0)


def test_compute_now_computes_in_the_calling_thread_and_counts_as_busy(mpeg2_ts, tmp_path):
    """バッチ処理は変換の後に compute_now で計算してから Done にする(2026-10-08)。計算中は
    busy(アプリを閉じられない)、止めるとそこまでの分を保存する。"""
    db = CutHistoryDB(tmp_path / "cut_history.db")
    with db.get_connection() as conn:
        rid = db.insert_recording(conn, {"file_name": "a.ts", "file_size": 1, "file_hash": "h", "created_at": "x"})
    worker = FeatureWorker(db=db)
    busy = []
    worker.compute_now(FeatureJob(rid, mpeg2_ts, FPS, [KeepRange(0, 100)]),
                       progress=lambda n: busy.append((worker.is_busy, worker.pending_count)))
    assert busy and all(b == (True, 1) for b in busy)
    assert not worker.is_busy and worker.pending_count == 0
    row = db.get_features_row(rid)
    assert row is not None and row["is_complete"] == 1 and row["frame_count"] == TOTAL

    stop = threading.Event()
    worker.compute_now(FeatureJob(rid, mpeg2_ts, FPS, []), stop_event=stop, progress=lambda n: stop.set())
    row = db.get_features_row(rid)
    assert row["is_complete"] == 0 and 0 < row["frame_count"] < TOTAL


def test_pictures_whose_time_goes_backwards_are_skipped_not_the_rest():
    """受信エラーの録画(_drop)では、壊れたコマの時刻が前のコマより前に出る(ある録画の
    490 コマ目は 2 コマ前と 1 コマ前の間)。そのコマだけ飛ばして続ける(2026-10-12 までは打ち切っていた)。"""
    step = 1001 / 30000
    pts = np.array([0, 1, 2, 3, 2.5, 4, 5, 5, 6, 3, 7], dtype=np.float64) * step
    positions = increasing_pictures(pts)
    assert positions.tolist() == [0, 1, 2, 3, 5, 6, 8, 10]
    positions = np.asarray(positions)
    idx, first = map_pictures_to_frames(pts[positions], 0.0, FPS)
    assert positions[np.asarray(idx)].tolist() == [0, 1, 2, 3, 5, 6, 8, 10]  # 飛ばしたコマは使わない
    assert np.asarray(first).all()
    assert increasing_pictures(np.zeros(0)).tolist() == []
