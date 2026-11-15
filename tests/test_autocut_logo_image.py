"""局のロゴ画像(.lgd)の材料 (設計書 §31.9「局のロゴの書き出し」、集める部分は 2026-10-13)。

半透明のロゴを平らな背景に重ねた合成のフレームで、材料から不透明度と色が戻ること、
録画ごとの材料を足しても同じになることを確かめる。特徴の計算では、ffmpeg で作った TS から
材料が集まり DB に入ることを確かめる(モックしない)。
"""

import subprocess

import numpy as np
import pytest

from ts_smartcut.autocut.feature_worker import FeatureJob, FeatureWorker
from ts_smartcut.autocut.features import KeepRange, compute_features
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.logo import LogoTemplate
from ts_smartcut.autocut.logo_image import (
    PAD,
    LogoSamples,
    accumulate,
    corner_geometry,
    logo_boxes,
    solve,
)

ALPHA = 0.4
COLOR = (220.0, 100.0, 160.0)  # Y・Cb・Cr
# 右上の隅(隅の中の座標、元の解像度)のロゴの位置
LOGO = (200, 40, 280, 72)  # x0, y0, x1, y1


def _template_for(geometry, x0, y0, x1, y1, corner=1) -> LogoTemplate:
    """ロゴの縁にマスクがある型(縮小した四隅の座標。1 画素 = 元の解像度の 8 画素)。"""
    hl, wl = geometry.height // 8, geometry.width // 8
    mask_x = np.zeros((4, hl, wl - 1), bool)
    mask_y = np.zeros((4, hl - 1, wl), bool)
    mask_x[corner, y0 // 8:y1 // 8, x0 // 8] = True
    mask_x[corner, y0 // 8:y1 // 8, x1 // 8 - 1] = True
    mask_y[corner, y0 // 8, x0 // 8:x1 // 8] = True
    mask_y[corner, y1 // 8 - 1, x0 // 8:x1 // 8] = True
    return LogoTemplate(np.zeros(mask_x.shape, np.float32), np.zeros(mask_y.shape, np.float32),
                        mask_x, mask_y, np.ones(4, np.float32), 100)


def _spool(path, geometry, levels, noisy=()):
    """背景の明るさが levels のフレームのモザイクを書く。noisy の番号のフレームは背景をまだらにする。"""
    rng = np.random.default_rng(1)
    w2, h2 = 2 * geometry.width, 2 * geometry.height
    x0, y0, x1, y1 = LOGO
    mx = geometry.width  # 右上の隅
    with open(path, "wb") as f:
        for i, level in enumerate(levels):
            planes = [np.full((h2, w2), level, np.float64),
                      np.full((h2 // 2, w2 // 2), 128.0 + (level - 128) / 4),
                      np.full((h2 // 2, w2 // 2), 128.0 - (level - 128) / 4)]
            if i in noisy:
                planes[0] += rng.normal(0, 30, planes[0].shape)
            for p, plane in enumerate(planes):
                d = 1 if p == 0 else 2
                region = plane[y0 // d:y1 // d, (mx + x0) // d:(mx + x1) // d]
                region[:] = (1 - ALPHA) * region + ALPHA * COLOR[p]
            f.write(b"".join(np.clip(np.round(p), 0, 255).astype(np.uint8).tobytes() for p in planes))


@pytest.fixture
def geometry():
    return corner_geometry(1280, 720, (80, 45), 4)  # 1280×720 を 1/4 にした特徴の四隅


def test_geometry_matches_the_feature_corners(geometry):
    assert (geometry.width, geometry.height) == (320, 180)
    assert geometry.origin(1) == (960, 0) and geometry.origin(3) == (960, 540)
    assert corner_geometry(1282, 720, (80, 45), 4) is None  # 1/4 で割り切れない


def test_box_covers_the_template_mask_with_padding(geometry):
    boxes = logo_boxes(_template_for(geometry, *LOGO), geometry)
    x0, y0, x1, y1 = LOGO
    # 右・下の縁のマスクは「ロゴの最後の画素と外の画素の間」なので、外の 1 画素(8 画素)まで入る
    assert boxes == [(1, x0 - PAD, y0 - PAD, x1 + 8 + PAD, y1 + 8 + PAD)]


def test_box_leaves_out_frame_lines_and_stray_edges(geometry):
    """番組の画面の枠(隅の上から下まで通る縦線・端から端までの横線)と離れた縁は範囲に入れない
    (CS-D の録画で範囲が隅全体になり、材料が取れなかった。2026-10-13)。"""
    template = _template_for(geometry, *LOGO)
    hl = geometry.height // 8
    mask_x, mask_y = np.asarray(template.mask_x), np.asarray(template.mask_y)
    mask_x[1, :, 2] = True  # 縦の枠線(隅の高さ全体)
    mask_x[1, 2:hl - 2, 30 // 8] = True  # 少し短い枠線(隅の高さの 3/4 以上)
    mask_y[1, hl - 3, :] = True  # 横の枠線
    mask_x[1, hl - 6, 4] = True  # ロゴから離れた 1 つの縁
    template = LogoTemplate(template.gx, template.gy, mask_x, mask_y, template.strength, template.frames)
    assert logo_boxes(template, geometry) == logo_boxes(_template_for(geometry, *LOGO), geometry)


def test_box_keeps_letters_a_little_apart_together(geometry):
    """文字の離れたロゴ(CLUSTER_GAP 以内)は1つの範囲。"""
    template = _template_for(geometry, *LOGO)
    second = _template_for(geometry, 296, 40, 312, 72)  # 右の文字(ロゴの右端から型で 2 画素)
    mask_x, mask_y = np.asarray(template.mask_x), np.asarray(template.mask_y)
    mask_x[1] |= np.asarray(second.mask_x)[1]
    mask_y[1] |= np.asarray(second.mask_y)[1]
    template = LogoTemplate(template.gx, template.gy, mask_x, mask_y, template.strength, template.frames)
    x0, y0, _, y1 = LOGO
    assert logo_boxes(template, geometry) == [(1, x0 - PAD, y0 - PAD, min(312 + 8 + PAD, geometry.width), y1 + 8 + PAD)]


def test_alpha_and_color_come_back_and_busy_backgrounds_are_skipped(geometry, tmp_path):
    levels = list(range(20, 240, 7))
    spool = tmp_path / "spool.yuv"
    _spool(spool, geometry, levels, noisy={3, 8})
    corners = accumulate(spool, len(levels), geometry, logo_boxes(_template_for(geometry, *LOGO), geometry))

    assert len(corners) == 1 and corners[0].n == len(levels) - 2  # まだらな背景の 2 枚は使わない
    solved = solve(corners)
    bx, by, _, _ = solved.box
    x0, y0, x1, y1 = LOGO
    ox = geometry.origin(1)[0]
    for p in range(3):
        d = 1 if p == 0 else 2
        inside = (slice((y0 - by) // d, (y1 - by) // d), slice((ox + x0 - bx) // d, (ox + x1 - bx) // d))
        alpha, color = np.asarray(solved.alpha[p]), np.asarray(solved.color[p])
        assert np.allclose(alpha[inside], ALPHA, atol=0.02)
        assert np.allclose(color[inside], COLOR[p], atol=3)
        outside = np.ones(alpha.shape, bool)
        outside[inside] = False
        assert alpha[outside].max() < 0.02


def test_samples_of_two_recordings_add_up_to_the_same_logo(geometry, tmp_path):
    levels = list(range(20, 240, 7))
    boxes = logo_boxes(_template_for(geometry, *LOGO), geometry)
    parts = []
    for i, chunk in enumerate((levels[::2], levels[1::2])):
        _spool(tmp_path / f"{i}.yuv", geometry, chunk)
        parts += accumulate(tmp_path / f"{i}.yuv", len(chunk), geometry, boxes)
    _spool(tmp_path / "all.yuv", geometry, levels)
    whole = solve(accumulate(tmp_path / "all.yuv", len(levels), geometry, boxes))
    merged = solve(parts)
    for p in range(3):
        assert np.allclose(np.asarray(merged.alpha[p]), np.asarray(whole.alpha[p]), atol=1e-9)


def test_samples_blob_round_trip(geometry, tmp_path):
    levels = list(range(20, 240, 11))
    _spool(tmp_path / "s.yuv", geometry, levels)
    corners = accumulate(tmp_path / "s.yuv", len(levels), geometry,
                         logo_boxes(_template_for(geometry, *LOGO), geometry))
    samples = LogoSamples((1280, 720), len(levels), corners)
    back = LogoSamples.from_blob(samples.to_blob())
    assert (back.frame_size, back.pictures, back.version) == ((1280, 720), len(levels), samples.version)
    assert back.corners[0].box == corners[0].box and back.corners[0].n == corners[0].n
    for p in range(3):
        assert np.array_equal(back.corners[0].sum_bv[p], corners[0].sum_bv[p])


# ---- 特徴の計算(実際の ffmpeg) ---------------------------------------------

FPS = 30000 / 1001
TOTAL = 300
CM = (100, 200)  # ロゴの無い区間


@pytest.fixture(scope="module")
def flat_logo_ts(tmp_path_factory):
    """1280×720、平らな背景の明るさがフレームごとに変わり、本編(CM 以外)だけ右上に半透明の白い四角。"""
    path = tmp_path_factory.mktemp("logo_image") / "flat_logo.ts"
    a, b = CM
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30000/1001,format=yuv420p,"
                                 "geq=lum='40+mod(N*37\\,160)':cb=128:cr=128",
            "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000",
            "-vf", f"drawbox=x=1080:y=40:w=140:h=70:t=fill:color=white@0.5:enable='not(between(n,{a},{b - 1}))'",
            "-frames:v", str(TOTAL), "-t", f"{TOTAL / FPS:.3f}",
            # 背景が毎フレーム変わるので、場面の切り替わりで I フレームを足さないようにする
            "-c:v", "mpeg2video", "-q:v", "2", "-g", "15", "-bf", "2", "-sc_threshold", "1000000000",
            "-c:a", "aac", "-b:a", "96k", str(path),
        ],
        check=True,
    )
    return path


def _kept_keyframes(path) -> int:
    """残した区間(CM 以外)の I フレームの数(15 フレームごと。エンコーダーは最後のフレームも I にする)。"""
    types = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "frame=pict_type", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True, check=True,
    ).stdout.split()
    return sum(1 for n, t in enumerate(types) if t.startswith("I") and not CM[0] <= n < CM[1])


def test_features_collect_logo_samples_from_kept_keyframes(flat_logo_ts):
    a, b = CM
    f = compute_features(flat_logo_ts, FPS, [KeepRange(0, a), KeepRange(b, TOTAL)])

    samples = f.logo_samples
    assert samples is not None and samples.frame_size == (1280, 720)
    assert samples.pictures == _kept_keyframes(flat_logo_ts)
    assert [c.corner for c in samples.corners] == [1]
    solved = solve(samples.corners)
    bx, by, bw, bh = solved.box
    assert bx <= 1080 and by <= 40 and bx + bw >= 1220 and by + bh >= 110
    inside = np.asarray(solved.alpha[0])[40 - by + 4:110 - by - 4, 1080 - bx + 4:1220 - bx - 4]
    assert np.allclose(inside, 0.5, atol=0.06)
    assert np.allclose(np.asarray(solved.color[0])[40 - by + 4:110 - by - 4, 1080 - bx + 4:1220 - bx - 4], 235, atol=12)


def test_no_logo_samples_before_cutting(flat_logo_ts):
    f = compute_features(flat_logo_ts, FPS, [])
    assert f.logo_samples is None


def test_worker_saves_logo_samples(flat_logo_ts, tmp_path):
    db = CutHistoryDB(tmp_path / "h.db")
    with db.get_connection() as conn:
        conn.execute("INSERT INTO recordings (id, file_name, file_size, file_hash, created_at, service_id) "
                     "VALUES (1, 'a.ts', 10, 'h', 'x', 1024)")
    worker = FeatureWorker(db=db)
    worker.compute_now(FeatureJob(1, flat_logo_ts, FPS, [KeepRange(0, CM[0]), KeepRange(CM[1], TOTAL)], 1024))

    rows = db.get_station_logo_samples(None, 1024)
    assert len(rows) == 1 and rows[0]["pictures"] == _kept_keyframes(flat_logo_ts)
    assert LogoSamples.from_blob(rows[0]["data"]).corners[0].corner == 1
