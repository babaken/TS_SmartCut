"""局ロゴ画像 (.lgd) の書き出し (設計書 §31.9「局のロゴの書き出し」、2026-10-30)。

材料は test_autocut_logo_image.py と同じ合成のフレーム (平らな背景に、右上の隅だけ不透明度 ALPHA・色 COLOR の
ロゴ) から本物の計算 (logo_image.accumulate) で作り、記録 DB に入れる。書いた .lgd は、標準の部分を取り込みの
読み方 (lgd_import.read_lgd) で、エンコードソフトの拡張 (見出し 540 バイトと係数 A・B) をその形のとおりに読み戻し、
位置・大きさ・不透明度・色・サービス ID が戻ることを確かめる (モックしない)。

書き出す隅と範囲は局の型で決める (2026-10-30 の修正 1〜4)。局の型は、録画ごとの特徴にロゴの型 (右上の LOGO の
縁) を入れて記録 DB で作る (logo.update_station_logo)。
"""

import struct

import numpy as np
import pytest

from tests.test_autocut_logo_image import ALPHA, COLOR, LOGO, _spool, _template_for
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.lgd_export import (
    LOGO_ALPHA,
    MIN_PIXEL_FRAMES,
    SKIP_DARK_OPAQUE,
    SKIP_DISAGREE,
    SKIP_EVERY_CORNER,
    SKIP_NO_STATION_TEMPLATE,
    SKIP_NOT_A_LOGO_CORNER,
    SKIP_OTHER_SIZE,
    SKIP_TOO_LARGE,
    cp932_name,
    crop,
    dark_opaque,
    export_station_lgd,
    exportable,
    lgd_bytes,
    sample_checks,
    station_logo_images,
    station_logo_report,
)
from ts_smartcut.autocut.lgd_import import read_lgd, service_id_from_name, sync_logo_folder, template_from_lgd
from ts_smartcut.autocut.logo import LogoTemplate, load_station_logo, rebuild_station_logo, update_station_logo
from ts_smartcut.autocut.logo_image import (
    SAMPLES_VERSION,
    LogoSamples,
    SolvedLogo,
    accumulate,
    corner_geometry,
    logo_boxes,
    solve,
)
from ts_smartcut.autocut.packed import Packed

ONID, SID = 4, 211
LEVELS = list(range(20, 240, 5))  # 44 枚 (MIN_PIXEL_FRAMES 以上)
C_SCALE = 4096.0 / 224.0  # 8 ビットの Cb・Cr → AviUtl の YC48


def _samples(tmp_path, size=(1280, 720), levels=LEVELS, name="s"):
    """右上にロゴのある合成のフレームの材料 (映像の大きさ size)。"""
    geometry = corner_geometry(size[0], size[1], (size[0] // 16, size[1] // 16), 4)
    spool = tmp_path / f"{name}.yuv"
    _spool(spool, geometry, levels)
    corners = accumulate(spool, len(levels), geometry, logo_boxes(_template_for(geometry, *LOGO), geometry))
    return LogoSamples(size, len(levels), corners), geometry


def _geometry(size):
    return corner_geometry(size[0], size[1], (size[0] // 16, size[1] // 16), 4)


def _logo_template(size=(1280, 720), boxes=((1, LOGO),)):
    """boxes の (隅, 範囲) の縁がいつも同じ向きのロゴの型 (録画の特徴に入れる型)。"""
    geometry = _geometry(size)
    mask_x = mask_y = None
    for corner, box in boxes:
        t = _template_for(geometry, *box, corner=corner)
        tx, ty = np.asarray(t.mask_x), np.asarray(t.mask_y)
        mask_x = tx if mask_x is None else mask_x | tx
        mask_y = ty if mask_y is None else mask_y | ty
    return LogoTemplate(mask_x.astype(np.float32), mask_y.astype(np.float32), mask_x, mask_y,
                        np.ones(4, np.float32), 100)


def _add(db, samples, day, template=None):
    """カットした録画 1 本 (その日の 01:00) と、ロゴの型の入った特徴とロゴ画像の材料。局の型を作り直す。"""
    from tests.test_autocut_logo_change import _blob

    start = f"2026-10-{day:02d}T01:00:00+09:00"
    with db.get_connection() as conn:
        rid = db.insert_recording(conn, {
            "file_name": f"{day}.ts", "file_size": 1, "file_hash": f"h{day}", "service_id": SID,
            "original_network_id": ONID, "service_name": "局", "recording_start": start,
            "created_at": start, "updated_at": start,
        })
    template = template if template is not None else _logo_template(tuple(samples.frame_size))
    db.save_features(rid, _blob(template), 3, True, 100)
    db.save_logo_samples(rid, samples.to_blob(), SAMPLES_VERSION, samples.pictures)
    update_station_logo(db, rid)
    return rid


def _extension(data: bytes, logo):
    """エンコードソフトの拡張: 見出し (識別値・版・幅・高さ・色差の間引き 2 つ・映像の幅・高さ・x・y・名前・
    サービス ID) と係数 (aY, bY, aU, bU, aV, bV)。"""
    offset = 32 + 48 + logo.width * logo.height * 12
    head = struct.unpack_from("<10i", data, offset)
    name = data[offset + 40:offset + 40 + 255].split(b"\0")[0].decode("cp932")
    (service_id,) = struct.unpack_from("<i", data, offset + 296)
    w, h, lx, ly = head[2], head[3], head[4], head[5]
    wuv, huv = w >> lx, h >> ly
    floats = np.frombuffer(data, "<f4", offset=offset + 540)
    sizes = [w * h, w * h, wuv * huv, wuv * huv, wuv * huv, wuv * huv]
    assert len(floats) == sum(sizes)
    planes, k = [], 0
    for size, shape in zip(sizes, [(h, w)] * 2 + [(huv, wuv)] * 4):
        planes.append(floats[k:k + size].reshape(shape))
        k += size
    return head, name, service_id, planes


@pytest.fixture
def db(tmp_path):
    return CutHistoryDB(tmp_path / "cut_history.db")


def test_exported_lgd_has_the_logo_in_the_standard_part_and_the_extension(db, tmp_path):
    samples, geometry = _samples(tmp_path)
    for day in (1, 2):
        _add(db, samples, day)
    written = export_station_lgd(db, ONID, SID, "テスト局", tmp_path / "out")

    assert [p.name for p in written] == [f"SID{SID}-1.lgd"]
    assert service_id_from_name(written[0]) == SID
    logos = read_lgd(written[0])
    assert len(logos) == 1
    lgd = logos[0]
    assert lgd.name == "テスト局(2026-10-02)" and lgd.date == "2026-10-02"
    assert (lgd.image_width, lgd.image_height) == (1280, 720)  # 拡張の映像の大きさ
    x0, y0, x1, y1 = LOGO
    ox = geometry.origin(1)[0]
    # ロゴの範囲 (型のマスクの外接矩形 + 余白) はロゴを含み、フレームの中にある
    assert lgd.x <= ox + x0 and lgd.y <= y0 and lgd.x + lgd.width >= ox + x1 and lgd.y + lgd.height >= y1
    assert lgd.x + lgd.width <= 1280 and lgd.x % 2 == 0 and lgd.y % 2 == 0
    # 範囲は局の型のマスクから決めたもの (修正 2)
    (_c, bx0, by0, bx1, by1), = logo_boxes(load_station_logo(db, ONID, SID), geometry)
    assert (lgd.x, lgd.y, lgd.width, lgd.height) == (ox + bx0, by0, bx1 - bx0, by1 - by0)
    inside = (slice(y0 - lgd.y, y1 - lgd.y), slice(ox + x0 - lgd.x, ox + x1 - lgd.x))
    alpha, luma = np.asarray(lgd.alpha), np.asarray(lgd.luma)
    assert np.allclose(alpha[inside], ALPHA, atol=0.02)
    assert np.allclose(luma[inside], COLOR[0], atol=3)
    outside = np.ones(alpha.shape, bool)
    outside[inside] = False
    assert alpha[outside].max() == 0.0  # 背景だけの画素は不透明度 0

    # 標準の部分の Cb・Cr (read_lgd は読まないので、画素のバイトを直接見る)
    data = written[0].read_bytes()
    pixels = np.frombuffer(data, "<i2", lgd.width * lgd.height * 6, 32 + 48).reshape(lgd.height, lgd.width, 6)
    for p, offset in ((1, 2), (2, 4)):
        assert np.allclose(pixels[inside][..., offset] / 1000.0, ALPHA, atol=0.02)
        assert np.allclose(pixels[inside][..., offset + 1] / C_SCALE + 128.0, COLOR[p], atol=3)

    # 拡張: 背景 = A × 見える値 + B (8 ビット / 255)。不透明度 = 1 − 1/A、色 = −B / (A − 1)
    head, name, service_id, (a_y, b_y, a_u, b_u, a_v, b_v) = _extension(data, lgd)
    assert head[:2] == (0x12345, 1)
    assert head[2:] == (lgd.width, lgd.height, 1, 1, 1280, 720, lgd.x, lgd.y)
    assert name == "テスト局(2026-10-02)" and service_id == SID
    for p, (a, b) in enumerate(((a_y, b_y), (a_u, b_u), (a_v, b_v))):
        d = 1 if p == 0 else 2
        region = (slice(inside[0].start // d, inside[0].stop // d), slice(inside[1].start // d, inside[1].stop // d))
        assert np.allclose(1.0 - 1.0 / a[region], ALPHA, atol=0.02)
        assert np.allclose(-b[region] / (a[region] - 1.0) * 255.0, COLOR[p], atol=3)
        rest = np.ones(a.shape, bool)
        rest[region] = False
        assert (a[rest] == 1.0).all() and (b[rest] == 0.0).all()  # ロゴの無い画素


def test_exported_lgd_can_be_imported_again(db, tmp_path):
    samples, _ = _samples(tmp_path)
    for day in (1, 2):
        _add(db, samples, day)
    path = export_station_lgd(db, ONID, SID, "局", tmp_path / "out")[0]
    template = template_from_lgd(read_lgd(path)[0])
    assert template is not None and template.has_logo == [False, True, False, False]  # 右上


def test_one_recording_is_not_exported(db, tmp_path):
    samples, _ = _samples(tmp_path)
    _add(db, samples, 1)
    assert export_station_lgd(db, ONID, SID, "局", tmp_path / "out") == []
    assert not (tmp_path / "out").exists()


def test_only_corners_with_a_logo_in_the_station_template_are_exported(db, tmp_path):
    """修正 1: 材料が右上にあっても、局の型でロゴがあるのが左上だけなら書き出さない。"""
    samples, _ = _samples(tmp_path)
    for day in (1, 2):
        _add(db, samples, day, template=_logo_template(boxes=((0, LOGO),)))
    images, skipped = station_logo_report(db, ONID, SID)
    assert images == [] and [(s.corner, s.reason) for s in skipped] == [(1, SKIP_NOT_A_LOGO_CORNER)]


def test_without_a_station_template_nothing_is_exported(db, tmp_path):
    samples, _ = _samples(tmp_path)
    for day in (1, 2):
        _add(db, samples, day)
    db.start_station_logo_generation(ONID, SID, "2026-10-03T00:00:00+09:00")  # 作り直し中 (型が無い)
    rid = _add(db, samples, 3)
    assert rid and load_station_logo(db, ONID, SID) is None
    images, skipped = station_logo_report(db, ONID, SID)
    assert images == [] and [s.reason for s in skipped] == [SKIP_NO_STATION_TEMPLATE]


def test_a_template_with_a_logo_in_every_corner_uses_the_imported_lgd_corner(db, tmp_path):
    """修正 3: 局の型が 4 隅ともロゴありなら、取り込んだ .lgd の隅だけ。取り込んだ .lgd が無ければ書き出さない。"""
    from tests.test_autocut_lgd_import import _ring_alpha, write_lgd

    samples, _ = _samples(tmp_path)
    everywhere = _logo_template(boxes=tuple((c, LOGO) for c in range(4)))
    for day in (1, 2):
        _add(db, samples, day, template=everywhere)
    assert all(load_station_logo(db, ONID, SID).has_logo)
    images, skipped = station_logo_report(db, ONID, SID)
    assert images == [] and [(s.corner, s.reason) for s in skipped] == [(1, SKIP_EVERY_CORNER)]

    folder = tmp_path / "Logo"
    folder.mkdir()
    write_lgd(folder / f"SID{SID}-1.lgd", 1100, 50, _ring_alpha(80, 40, 6, 500), image_size=(1280, 720))  # 右上
    sync_logo_folder(db, folder)
    images, skipped = station_logo_report(db, ONID, SID)
    assert [i.corner for i in images] == [1] and skipped == []

    write_lgd(folder / f"SID{SID}-1.lgd", 40, 40, _ring_alpha(80, 40, 6, 500), image_size=(1280, 720))  # 左上
    sync_logo_folder(db, folder)
    images, skipped = station_logo_report(db, ONID, SID)
    assert images == [] and [(s.corner, s.reason) for s in skipped] == [(1, SKIP_EVERY_CORNER)]


def test_a_template_box_covering_most_of_the_corner_is_not_exported(db, tmp_path):
    """修正 2: 局の型の範囲が隅の大部分 (幅・高さの 3/4 以上) なら書き出さない。隅の端から端まで通る線は
    番組の枠として範囲の前に除くので、ここでは線の短い大きな枠 (隅の 7 割ほど) にする。"""
    samples, geometry = _samples(tmp_path)
    assert (geometry.width, geometry.height) == (320, 180)
    wide = (8, 8, 236, 132)
    for day in (1, 2):
        _add(db, samples, day, template=_logo_template(boxes=((1, wide),)))
    images, skipped = station_logo_report(db, ONID, SID)
    assert images == [] and [(s.corner, s.reason) for s in skipped] == [(1, SKIP_TOO_LARGE)]


def test_dark_opaque_pixels_are_a_band_not_a_logo():
    """修正 4: ほぼ不透明で暗い画素がロゴの画素の半分以上なら、帯・暗い背景とみなす。"""
    shape = (20, 40)
    frames = [np.full(shape, 100.0), np.full((10, 20), 100.0), np.full((10, 20), 100.0)]

    def solved(alpha, luma):
        return SolvedLogo((0, 0, 40, 20), [np.full(shape, alpha), np.zeros((10, 20)), np.zeros((10, 20))],
                          [np.full(shape, luma), np.full((10, 20), 128.0), np.full((10, 20), 128.0)], frames)

    assert dark_opaque(solved(0.95, 26.0))  # 黒い帯
    assert not dark_opaque(solved(0.4, 220.0))  # 半透明の白いロゴ
    assert not dark_opaque(solved(0.95, 230.0))  # 不透明な白いロゴ
    assert not dark_opaque(solved(0.0, 0.0))  # ロゴの画素が無い
    half = solved(0.4, 220.0)
    half.alpha[0][:, :20], half.color[0][:, :20] = 0.95, 20.0  # 半分が黒い帯
    assert dark_opaque(half)


def _remade(samples, alpha, color):
    """samples の右上の材料と同じ範囲・背景の明るさ・フレーム数で、不透明度 alpha(面ごとの大きさ (h, w)、d) と
    色 color の材料を作り直したもの。"""
    from ts_smartcut.autocut.logo_image import CornerSamples

    corner = samples.corners[0]
    _x, _y, w, h = corner.box
    levels = np.array(LEVELS, np.float64)
    backgrounds = [levels, 128.0 + (levels - 128) / 4, 128.0 - (levels - 128) / 4]  # _spool と同じ
    sum_v, sum_bv = [], []
    for p in range(3):
        d = 1 if p == 0 else 2
        a = alpha((h // d, w // d), d)
        seen = (1 - a) * backgrounds[p][:, None, None] + a * color[p]
        sum_v.append(seen.sum(axis=0))
        sum_bv.append((backgrounds[p][:, None, None] * seen).sum(axis=0))
    sums = np.array([b.sum() for b in backgrounds]), np.array([(b * b).sum() for b in backgrounds])
    remade = CornerSamples(corner.corner, corner.box, len(LEVELS), sums[0], sums[1], sum_v, sum_bv)
    return LogoSamples(samples.frame_size, samples.pictures, [remade])


def _shifted(samples, dy=16, dx=24):
    """ロゴの位置だけずらした材料。録画の型が番組の枠などを拾い、背景を測る輪がロゴから離れて値がずれた録画の代わり。"""
    def alpha(shape, d):
        a = np.zeros(shape)
        a[(16 + dy) // d:(48 + dy) // d, (16 + dx) // d:(96 + dx) // d] = ALPHA
        return a

    return _remade(samples, alpha, COLOR)


def test_a_recording_that_disagrees_with_the_others_is_left_out(db, tmp_path):
    """修正 5: 3 本と合わない 1 本を除いて足す (足すとロゴの外に不透明度が出る)。"""
    samples, geometry = _samples(tmp_path)
    for day in (1, 2, 3):
        _add(db, samples, day)
    _add(db, _shifted(samples), 4)
    image, = station_logo_images(db, ONID, SID)
    assert image.recordings == 3
    x0, y0, x1, y1 = LOGO
    ox = geometry.origin(1)[0]
    sx, sy, _w, _h = image.solved.box
    inside = (slice(y0 - sy, y1 - sy), slice(ox + x0 - sx, ox + x1 - sx))
    alpha = np.asarray(image.solved.alpha[0])
    assert np.allclose(alpha[inside], ALPHA, atol=0.02)
    outside = np.ones(alpha.shape, bool)
    outside[inside] = False
    assert alpha[outside].max() < LOGO_ALPHA  # ずれた録画のロゴは入らない


def test_two_recordings_that_disagree_are_not_exported(db, tmp_path):
    samples, _ = _samples(tmp_path)
    _add(db, samples, 1)
    _add(db, _shifted(samples), 2)
    images, skipped = station_logo_report(db, ONID, SID)
    assert images == [] and [(s.corner, s.reason) for s in skipped] == [(1, SKIP_DISAGREE)]


def test_sample_checks_leave_out_few_frames_and_dark_recordings(tmp_path):
    """修正 5 の録画ごとの判定: フレームの少ない録画 (1)、暗く不透明な録画 (2) は比べる前に除く。"""
    samples, _ = _samples(tmp_path)
    few, _ = _samples(tmp_path, levels=LEVELS[:MIN_PIXEL_FRAMES - 1], name="few")
    dark = _remade(samples, lambda shape, d: np.full(shape, 0.95), (20.0, 128.0, 128.0))  # ほぼ不透明で暗い帯
    items = [samples.corners[0], few.corners[0], dark.corners[0], samples.corners[0]]
    box = items[0].box
    assert sample_checks(items, box) == [0, 1, 2, 0]


def test_crop_cuts_every_plane_to_the_overlap():
    planes = [np.arange(20 * 40, dtype=np.float64).reshape(20, 40), np.zeros((10, 20)), np.ones((10, 20))]
    solved = SolvedLogo((100, 10, 40, 20), planes, planes, planes)
    cut = crop(solved, (120, 0, 100, 20))
    assert cut.box == (120, 10, 20, 10)
    assert cut.alpha[0].tolist() == planes[0][:10, 20:].tolist() and cut.alpha[1].shape == (5, 10)
    assert crop(solved, (0, 0, 50, 50)) is None


def test_too_few_flat_frames_are_not_exported(db, tmp_path):
    few, _ = _samples(tmp_path, levels=LEVELS[:MIN_PIXEL_FRAMES // 2 - 1], name="few")
    for day in (1, 2):
        _add(db, few, day)
    assert station_logo_images(db, ONID, SID) == []  # 2 本を足しても平らな背景のフレームが MIN_PIXEL_FRAMES に足りない


def test_only_the_current_logo_generation_and_samples_version_are_used(db, tmp_path):
    samples, _ = _samples(tmp_path)
    _add(db, samples, 1)
    _add(db, samples, 2)
    _add(db, samples, 3)
    assert station_logo_images(db, ONID, SID)[0].recordings == 3
    db.start_station_logo_generation(ONID, SID, "2026-10-02T00:00:00+09:00")
    rebuild_station_logo(db, ONID, SID)
    assert station_logo_images(db, ONID, SID)[0].recordings == 2
    rid = _add(db, samples, 4)
    db.save_logo_samples(rid, samples.to_blob(), SAMPLES_VERSION + 1, samples.pictures)  # 別の版の材料は使わない
    assert station_logo_images(db, ONID, SID)[0].recordings == 2


def test_only_the_station_template_size_is_exported_and_nothing_is_overwritten(db, tmp_path):
    """局の型は新しい録画の映像の大きさで作るので、書き出すのもその大きさだけ (前の大きさの材料は other_size)。"""
    small, _ = _samples(tmp_path, (1280, 720), name="a")
    large, _ = _samples(tmp_path, (1920, 1080), name="b")
    for day, s in ((1, small), (2, small), (3, large), (4, large)):
        _add(db, s, day)
    out = tmp_path / "out"
    out.mkdir()
    (out / f"SID{SID}-1.lgd").write_bytes(b"someone else's file")

    written = export_station_lgd(db, ONID, SID, "局", out)
    assert [p.name for p in written] == [f"SID{SID}-2.lgd"]
    assert (out / f"SID{SID}-1.lgd").read_bytes() == b"someone else's file"
    lgd = read_lgd(written[0])[0]
    assert (lgd.image_width, lgd.image_height) == (1920, 1080)
    assert 1920 * 3 // 4 <= lgd.x and lgd.x + lgd.width <= 1920  # 右上の隅の中
    _images, skipped = station_logo_report(db, ONID, SID)
    assert [(s.frame_size, s.reason) for s in skipped] == [((1280, 720), SKIP_OTHER_SIZE)]


def test_long_names_are_cut_at_a_character_boundary():
    data = cp932_name("あ" * 20, 31)  # cp932 で 2 バイトずつ: 31 バイトに 15 文字
    assert data.decode("cp932") == "あ" * 15
    assert cp932_name("あ" * 200, 254).decode("cp932") == "あ" * 127


def test_long_names_fit_in_both_name_fields(tmp_path):
    samples, _ = _samples(tmp_path)
    path = tmp_path / "long.lgd"
    path.write_bytes(lgd_bytes("局" * 40, solve(samples.corners), SID, (1280, 720)))
    lgd = read_lgd(path)[0]
    assert lgd.name == "局" * 15 and lgd.image_width == 1280
    _head, name, service_id, _planes = _extension(path.read_bytes(), lgd)
    assert name == "局" * 40 and service_id == SID


def test_exportable_needs_frames_and_logo_pixels(tmp_path):
    samples, _ = _samples(tmp_path)
    solved = solve(samples.corners)
    assert exportable(solved)
    solved.alpha[0] = Packed.zeros(solved.alpha[0].shape, "f8")  # ロゴの画素が無い
    assert not exportable(solved)


def test_header_matches_the_logo_file_format(db, tmp_path):
    samples, _ = _samples(tmp_path)
    for day in (1, 2):
        _add(db, samples, day)
    data = export_station_lgd(db, ONID, SID, "局", tmp_path / "out")[0].read_bytes()
    assert data[:23] == b"<logo data file ver0.1>" and data[23:31] == b"\0" * 8 and data[31] == 1
    x, y, h, w, fade_in, fade_out, start, end = struct.unpack_from("<8h", data, 32 + 32)
    assert (fade_in, fade_out, start, end) == (0, 0, 0, 0)
    assert len(data) == 32 + 48 + w * h * 12 + 540 + (2 * w * h + 4 * (w // 2) * (h // 2)) * 4


def test_settings_tab_exports_the_selected_station(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from tests.test_autocut_logo_change import _add as add_recording
    from tests.test_autocut_logo_change import _template
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    db = CutHistoryDB(tmp_path / "cut_history.db")
    template = _template(tmp_path, (1080, 40))
    samples, _ = _samples(tmp_path)
    for day in (1, 2):
        rid = add_recording(db, template, day)
        db.save_logo_samples(rid, samples.to_blob(), SAMPLES_VERSION, samples.pictures)
    dialog = SettingsDialog(AppSettings(), history_db=db)
    panel = dialog.autocut_panel
    assert not panel.export_lgd_button.isEnabled()
    panel.station_table.selectRow(0)
    assert panel.export_lgd_button.isEnabled()

    out = tmp_path / "out"
    out.mkdir()
    shown = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(out))
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: shown.append(text))
    panel._on_export_lgd()
    assert [p.name for p in out.iterdir()] == [f"SID{SID}-1.lgd"]
    assert str(out / f"SID{SID}-1.lgd") in shown[0]

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")  # 取り消し
    panel._on_export_lgd()
    assert len(shown) == 1
