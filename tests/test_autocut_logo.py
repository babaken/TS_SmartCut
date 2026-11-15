"""ロゴの型とフレームごとの一致度 (自動カット計画 A4 の案A)。

本編の区間だけ隅に半透明のロゴを重ねた合成の映像で、型ができること、本編で
一致度が高く CM で低いことを確かめる(モックしない)。
"""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from ts_smartcut.autocut.features import KeepRange, RecordingFeatures, compute_features
from ts_smartcut.autocut.logo import scores_from_corners, template_from_corners


def score(corners, template):
    """四隅 (k, 4, 40, 60) の一致度 0〜1 (保存する 0〜255 を戻したもの)。"""
    return np.asarray(scores_from_corners(corners, template)) / 255.0

FPS = 30000 / 1001
TOTAL = 300
CM = (100, 200)  # ロゴの無い区間(CM)


def _corners(rng, k, logo: bool, still=None):
    """背景がフレームごとに変わる四隅 (k, 4, 40, 60)。logo なら右上(1番)に半透明の白い枠。"""
    c = rng.integers(40, 200, size=(k, 4, 40, 60)).astype(np.float32)
    c = (c + np.roll(c, 1, axis=3) + np.roll(c, 1, axis=2)) / 3  # 少しなめらかにする
    if still is not None:
        c[:, :] = still  # 動かない映像(強い縁)
    if logo:
        box = np.zeros((40, 60), bool)
        box[10:30, 20:50] = True
        box[14:26, 24:46] = False
        c[:, 1][:, box] = c[:, 1][:, box] * 0.5 + 255 * 0.5
    return np.clip(c, 0, 255)


def test_template_finds_the_logo_and_ignores_a_still_image_in_the_kept_range():
    rng = np.random.default_rng(1)
    still = np.zeros((40, 60), np.float32)
    still[:, 30:] = 220  # 縦の強い縁(エンドカードのような静止画。残した区間の 2 割)
    program = _corners(rng, 160, logo=True)
    end_card = _corners(rng, 40, logo=False, still=still)
    cm = _corners(rng, 100, logo=False)

    template = template_from_corners(np.concatenate([program, end_card]))
    assert template.has_logo == [False, True, False, False]
    assert template.frames == 200

    s_program = score(program, template)[:, 1]
    s_cm = score(cm, template)[:, 1]
    s_card = score(end_card, template)[:, 1]
    assert s_program.mean() > 0.8
    assert s_cm.mean() < 0.1 and s_card.mean() < 0.1
    assert np.all(score(program, template)[:, [0, 2, 3]] == 0)


def test_no_logo_when_the_kept_range_has_none():
    rng = np.random.default_rng(2)
    template = template_from_corners(_corners(rng, 100, logo=False))
    assert not any(template.has_logo)
    assert np.all(score(_corners(rng, 10, logo=True), template) == 0)


def test_empty_kept_range_gives_no_template():
    template = template_from_corners(np.zeros((0, 4, 40, 60), np.uint8))
    assert template.frames == 0 and not any(template.has_logo)


@pytest.fixture(scope="module")
def logo_ts(tmp_path_factory):
    """1280×720、背景はフレームごとのノイズ、本編(CM 以外)だけ右上に半透明の白い枠。"""
    path = tmp_path_factory.mktemp("logo") / "logo.ts"
    a, b = CM
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30000/1001,format=yuv420p,noise=alls=40:allf=t",
            "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000",
            "-vf", f"drawbox=x=1080:y=40:w=140:h=70:t=10:color=white@0.5:enable='not(between(n,{a},{b - 1}))'",
            "-frames:v", str(TOTAL), "-t", f"{TOTAL / FPS:.3f}",
            "-c:v", "mpeg2video", "-q:v", "6", "-g", "15", "-bf", "2",
            "-c:a", "aac", "-b:a", "96k", str(path),
        ],
        check=True,
    )
    return path


def test_logo_score_is_high_in_the_program_and_low_in_the_cm(logo_ts):
    a, b = CM
    f = compute_features(logo_ts, FPS, [KeepRange(0, a), KeepRange(b, TOTAL)])
    assert f.complete
    assert f.logo_template is not None
    assert f.logo_template.has_logo == [False, True, False, False]
    assert f.logo_score.shape == (4, f.frame_count)

    s = np.asarray(f.logo_score[1], np.float32) / 255
    # 境目の前後数フレームは除く(GOP の参照で枠が残る・崩れることがある)
    assert s[5:a - 5].mean() > 0.6 and s[b + 5:TOTAL - 5].mean() > 0.6
    assert s[a + 5:b - 5].mean() < 0.1
    # 境目のフレームで切り替わる
    assert s[a - 3:a].min() > 0.3 and s[a + 1:a + 4].max() < 0.2

    # 保存と読み込みで型と一致度が変わらない
    g = RecordingFeatures.from_blob(f.to_blob())
    assert np.array_equal(g.logo_score, f.logo_score)
    assert g.logo_template.has_logo == f.logo_template.has_logo
    assert np.array_equal(g.logo_template.mask_x, f.logo_template.mask_x)
    assert np.allclose(g.logo_template.gx, f.logo_template.gx, atol=1e-3)


def test_old_feature_blobs_without_logo_still_load(logo_ts):
    f = compute_features(logo_ts, FPS, [KeepRange(0, TOTAL)])
    f.logo_template = None
    f.logo_score = np.zeros((4, 0), np.uint8)
    g = RecordingFeatures.from_blob(f.to_blob())
    assert g.logo_template is None and g.logo_score.shape == (4, 0)


# ---- 局の型 ------------------------------------------------------------------

from ts_smartcut.autocut.logo import (  # noqa: E402
    combine_templates,
    load_station_logo,
    recording_template_from_features,
    update_station_logo,
)


def _template(rng, logo=True, still=None, frames=100):
    return template_from_corners(_corners(rng, frames, logo=logo, still=still))


def test_station_template_needs_two_recordings_of_the_same_size():
    rng = np.random.default_rng(3)
    one = _template(rng)
    assert combine_templates([one]) is None
    other_size = template_from_corners(_corners(rng, 50, logo=True)[:, :, :20, :30])  # 型は 10 × 15
    assert combine_templates([other_size, one]) is None  # 大きさの違う古い録画は使わない
    station = combine_templates([one, _template(rng)])
    assert station is not None and station.has_logo == [False, True, False, False]
    assert station.frames == 200


def test_station_template_drops_a_mark_seen_in_only_one_recording():
    """1本の番組だけに出ていた表示(番組のマーク等)は局の型に入れない。"""
    rng = np.random.default_rng(4)
    mark = np.zeros((40, 60), np.float32)
    mark[:, 30:] = 220
    with_mark = _template(rng, logo=False, still=mark)
    assert all(with_mark.has_logo)  # その録画だけで見ると、どの隅にも「ロゴ」がある
    station = combine_templates([with_mark, _template(rng, logo=False)])
    assert station is not None and not any(station.has_logo)


def test_station_template_scores_a_new_recording(logo_ts):
    """カットする前の録画(残した区間なし)は、局の型で一致度を計算する。"""
    a, b = CM
    own = compute_features(logo_ts, FPS, [KeepRange(0, a), KeepRange(b, TOTAL)])
    station = combine_templates([own.logo_template, own.logo_template])
    f = compute_features(logo_ts, FPS, [], station_template=station)
    assert f.logo_from_station
    s = np.asarray(f.logo_score[1], np.float32) / 255
    assert s[5:a - 5].mean() > 0.6 and s[a + 5:b - 5].mean() < 0.1
    # 局の型で計算した特徴は、局の型を作る材料にしない
    blob = f.to_blob()
    assert recording_template_from_features(blob) is None
    assert RecordingFeatures.from_blob(blob).logo_from_station
    # 残した区間も局の型も無ければ一致度は 0
    assert not compute_features(logo_ts, FPS, []).logo_score.any()


def _add_recording(db, service_id, onid=4, start="2026-10-01T00:00:00+09:00"):
    with db.get_connection() as conn:
        return db.insert_recording(conn, {
            "file_name": f"{service_id}_{start}.ts", "file_size": 1, "file_hash": f"{service_id}{start}",
            "service_id": service_id, "original_network_id": onid, "recording_start": start,
            "created_at": start, "updated_at": start,
        })


def test_station_logo_is_updated_as_features_are_saved(logo_ts, tmp_path):
    from ts_smartcut.autocut.feature_worker import FeatureJob, FeatureWorker
    from ts_smartcut.autocut.history_db import CutHistoryDB

    a, b = CM
    db = CutHistoryDB(tmp_path / "cut_history.db")
    first = _add_recording(db, 211, start="2026-10-01T01:00:00+09:00")
    second = _add_recording(db, 211, start="2026-10-08T01:00:00+09:00")
    other = _add_recording(db, 211, onid=6, start="2026-10-05T01:00:00+09:00")  # 別のネットワークの同じ番号
    keep = [KeepRange(0, a), KeepRange(b, TOTAL)]
    worker = FeatureWorker(db=db)

    worker.submit([FeatureJob(first, logo_ts, FPS, keep, service_id=1)])
    assert worker.wait_idle(60)
    assert db.get_station_logo(4, 211) is None  # 1本ではまだ作らない
    worker.submit([FeatureJob(other, logo_ts, FPS, keep, service_id=1)])
    assert worker.wait_idle(60)
    assert db.get_station_logo(4, 211) is None and db.get_station_logo(6, 211) is None

    worker.submit([FeatureJob(second, logo_ts, FPS, keep, service_id=1)])
    assert worker.wait_idle(60)
    row = db.get_station_logo(4, 211)
    assert row is not None and row["recordings_count"] == 2
    assert (row["valid_from"], row["valid_to"]) == ("2026-10-01T01:00:00+09:00", "2026-10-08T01:00:00+09:00")
    station = load_station_logo(db, 4, 211)
    assert station.has_logo == [False, True, False, False]
    assert [r["service_id"] for r in db.list_station_logos()] == [211]

    # 作り直しても行は増えない。局の分からない録画は何もしない
    assert update_station_logo(db, second) is not None
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM logos").fetchone()[0] == 1
    unknown = _add_recording(db, None)
    assert update_station_logo(db, unknown) is None


def test_station_logo_restart_keeps_old_template_with_its_period(logo_ts, tmp_path):
    """ロゴが変わった時(A9 の「はい」): 古い型は期間付きで残り、新しい型は起点より後の録画だけで作る。"""
    from ts_smartcut.autocut.feature_worker import FeatureJob, FeatureWorker
    from ts_smartcut.autocut.history_db import CutHistoryDB
    from ts_smartcut.autocut.logo import restart_station_logo

    a, b = CM
    db = CutHistoryDB(tmp_path / "cut_history.db")
    starts = ["2026-10-01T01:00:00+09:00", "2026-10-08T01:00:00+09:00",
              "2026-10-15T01:00:00+09:00", "2026-10-22T01:00:00+09:00"]
    ids = [_add_recording(db, 211, start=s) for s in starts]
    keep = [KeepRange(0, a), KeepRange(b, TOTAL)]
    worker = FeatureWorker(db=db)
    worker.submit([FeatureJob(rid, logo_ts, FPS, keep, service_id=1) for rid in ids[:3]])
    assert worker.wait_idle(120)
    assert db.get_station_logo(4, 211)["recordings_count"] == 3

    # 3本目からロゴが変わった。新しい録画は1本なので、まだ型は無い
    assert restart_station_logo(db, 4, 211, since=starts[2]) is None
    current = db.get_station_logo(4, 211)
    assert current["is_current"] == 1 and current["logo_data"] is None and current["generation_start"] == starts[2]
    old = db.get_station_logo(4, 211, recorded_at=starts[1])
    # 古い型は起点より前の録画(1・2本目)だけで作り直して残す(2026-10-15。ロゴが変わった後の録画が混ざった型を残さない)
    assert old["is_current"] == 0 and (old["valid_from"], old["valid_to"]) == (starts[0], starts[1])
    assert old["recordings_count"] == 2
    assert load_station_logo(db, 4, 211) is None  # 今の型は作り直し中
    assert load_station_logo(db, 4, 211, recorded_at=starts[0]) is not None  # 変わる前の録画は古い型

    # 4本目を保存すると、3本目と4本目だけで今の型を作る(古い型は残る)
    worker.submit([FeatureJob(ids[3], logo_ts, FPS, keep, service_id=1)])
    assert worker.wait_idle(60)
    current = db.get_station_logo(4, 211)
    assert current["recordings_count"] == 2 and (current["valid_from"], current["valid_to"]) == (starts[2], starts[3])
    assert [r["is_current"] for r in db.list_station_logos()] == [0, 1]
