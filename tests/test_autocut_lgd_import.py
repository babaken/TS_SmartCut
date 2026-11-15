"""局ロゴ画像 (.lgd) の取り込み (設計書 §31.9、2026-10-15)。

.lgd はテストの中で作る(エンコードソフトの形: 標準の画素と、映像の大きさの入った拡張)。
本編の区間だけ隅に半透明の白い枠を重ねた合成の映像で、その枠を描いた .lgd の型で一致度が
本編で高く CM で低いことを確かめる(モックしない)。取り込みは Logo/ フォルダとの同期(録画を開くたび)。
"""

import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from ts_smartcut.autocut.features import compute_features
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.lgd_import import (
    MAX_FILE_SIZE,
    SKIP_NO_EDGES,
    SKIP_NO_IMAGE_SIZE,
    SKIP_NO_SERVICE_ID,
    SKIP_TOO_LARGE,
    SKIP_UNREADABLE,
    check_imported_logo,
    load_imported_logo,
    read_lgd,
    service_id_from_name,
    sync_logo_folder,
    template_from_lgd,
)

FPS = 30000 / 1001
TOTAL = 300
CM = (100, 200)  # ロゴの無い区間(CM)
WIDTH, HEIGHT = 1280, 720
BOX = (1080, 40, 140, 70, 10)  # x, y, 幅, 高さ, 線の太さ


def _ring_alpha(w, h, t, value):
    alpha = np.zeros((h, w), np.int16)
    alpha[:t, :] = alpha[-t:, :] = alpha[:, :t] = alpha[:, -t:] = value
    return alpha


def write_lgd(path: Path, x, y, alpha, luma_yc48=4096, name="テスト局(2026-01-02)",
              image_size=(WIDTH, HEIGHT), extension=True):
    """エンコードソフトの .lgd を書く。alpha は 0〜1000 の (高さ, 幅)。"""
    h, w = alpha.shape
    head = b"<logo data file ver0.1>".ljust(31, b"\0") + bytes([1])
    logo = name.encode("cp932").ljust(32, b"\0") + struct.pack("<8h", x, y, h, w, 0, 0, 0, 0)
    pixels = np.zeros((h, w, 6), "<i2")
    pixels[..., 0] = alpha
    pixels[..., 1] = luma_yc48
    data = head + logo + pixels.tobytes()
    if extension:
        data += struct.pack("<10i", 0x12345, 1, w, h, 1, 1, image_size[0], image_size[1], x, y)
        data += b"\0" * 64
    path.write_bytes(data)
    return path


def _box_lgd(path, **kwargs):
    x, y, w, h, t = BOX
    return write_lgd(path, x, y, _ring_alpha(w, h, t, 500), **kwargs)


@pytest.fixture(scope="module")
def logo_ts(tmp_path_factory):
    """1280×720、背景はフレームごとのノイズ、本編(CM 以外)だけ右上に半透明の白い枠。"""
    path = tmp_path_factory.mktemp("lgd") / "logo.ts"
    a, b = CM
    x, y, w, h, t = BOX
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=gray:s={WIDTH}x{HEIGHT}:r=30000/1001,format=yuv420p,noise=alls=40:allf=t",
            "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000",
            "-vf", f"drawbox=x={x}:y={y}:w={w}:h={h}:t={t}:color=white@0.5:enable='not(between(n,{a},{b - 1}))'",
            "-frames:v", str(TOTAL), "-t", f"{TOTAL / FPS:.3f}",
            "-c:v", "mpeg2video", "-q:v", "6", "-g", "15", "-bf", "2",
            "-c:a", "aac", "-b:a", "96k", str(path),
        ],
        check=True,
    )
    return path


def test_read_lgd_gives_position_alpha_colour_and_video_size(tmp_path):
    path = _box_lgd(tmp_path / "SID211-1.lgd")
    [logo] = read_lgd(path)
    assert (logo.x, logo.y, logo.width, logo.height) == (1080, 40, 140, 70)
    assert (logo.image_width, logo.image_height) == (WIDTH, HEIGHT)
    assert (logo.name, logo.date) == ("テスト局(2026-01-02)", "2026-01-02")
    assert np.asarray(logo.alpha).max() == pytest.approx(0.5) and logo.alpha[30, 70] == 0
    assert logo.luma[0, 0] == pytest.approx(235.0)  # 4096 = 8 ビットの 235
    assert service_id_from_name(path) == 211
    assert service_id_from_name(tmp_path / "logo.lgd") is None


def test_template_has_the_logo_in_its_corner_only(tmp_path):
    template = template_from_lgd(read_lgd(_box_lgd(tmp_path / "SID1-1.lgd"))[0])
    assert template.has_logo == [False, True, False, False]
    # 録画から作る型と同じ大きさ(1/4 の映像の 1/4 の隅を 1/2)
    assert template.gx.shape == (4, 720 // 4 // 4 // 2, 1280 // 4 // 4 // 2 - 1)


def _folder(tmp_path):
    folder = tmp_path / "Logo"
    folder.mkdir()
    return folder


def test_imported_logo_scores_the_program_high_and_the_cm_low(logo_ts, tmp_path):
    folder = _folder(tmp_path)
    _box_lgd(folder / "SID211-1.lgd")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    result = sync_logo_folder(db, folder)
    assert result.imported == ["SID211-1.lgd"] and result.skipped == [] and result.removed == []

    template = load_imported_logo(db, 211)
    f = compute_features(logo_ts, FPS, [], station_template=template)
    assert f.logo_from_station
    s = np.asarray(f.logo_score[1], np.float32) / 255
    a, b = CM
    assert s[5:a - 5].mean() > 0.6 and s[b + 5:TOTAL - 5].mean() > 0.6
    assert s[a + 5:b - 5].mean() < 0.1
    assert not np.asarray(f.logo_score)[[0, 2, 3]].any()


def test_the_newest_logo_of_the_station_is_used(tmp_path):
    folder = _folder(tmp_path)
    write_lgd(folder / "SID300-1.lgd", 1080, 40, _ring_alpha(140, 70, 10, 500), name="局(2024-10-17)")
    write_lgd(folder / "SID300-2.lgd", 60, 620, _ring_alpha(140, 70, 10, 500), name="局(2025-04-04)")
    write_lgd(folder / "SID300-3.lgd", 1080, 620, _ring_alpha(140, 70, 10, 500), name="局(2023-01-01)")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    sync_logo_folder(db, folder)
    assert db.get_imported_logo(300)["file_name"] == "SID300-2.lgd"
    assert load_imported_logo(db, 300).has_logo == [False, False, True, False]
    assert load_imported_logo(db, 999) is None


def test_sync_imports_only_new_and_changed_files_and_removes_deleted_ones(tmp_path):
    import os

    folder = _folder(tmp_path)
    first = _box_lgd(folder / "SID211-1.lgd")
    _box_lgd(folder / "SID141-1.lgd")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    assert sync_logo_folder(db, folder).imported == ["SID141-1.lgd", "SID211-1.lgd"]
    # 変わらなければ何もしない
    again = sync_logo_folder(db, folder)
    assert (again.imported, again.skipped, again.removed) == ([], [], [])
    # 作り直した(更新日時が変わった)ファイルだけ取り込み直す
    db.mark_imported_logo_stale_notified("SID211-1.lgd")
    write_lgd(first, 60, 40, _ring_alpha(140, 70, 10, 500))
    stat = first.stat()
    os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    assert sync_logo_folder(db, folder).imported == ["SID211-1.lgd"]
    assert load_imported_logo(db, 211).has_logo == [True, False, False, False]
    assert not db.get_imported_logo(211)["stale_notified"]  # 新しいファイルは知らせ直す
    # フォルダから消したファイルは DB からも消す
    (folder / "SID141-1.lgd").unlink()
    assert sync_logo_folder(db, folder).removed == ["SID141-1.lgd"]
    assert [r["file_name"] for r in db.list_imported_logos()] == ["SID211-1.lgd"]


def test_files_that_cannot_be_used_are_skipped_and_not_read_again(tmp_path):
    folder = _folder(tmp_path)
    _box_lgd(folder / "logo.lgd")  # ファイル名に局の番号が無い
    _box_lgd(folder / "SID2-1.lgd", extension=False)  # 映像の大きさが無い
    (folder / "SID3-1.lgd").write_bytes(b"not a logo")
    write_lgd(folder / "SID4-1.lgd", 1200, 40, _ring_alpha(140, 70, 10, 500))  # 映像の外にはみ出す
    write_lgd(folder / "SID5-1.lgd", 1080, 40, _ring_alpha(140, 70, 10, 0))  # 透明(縁が無い)
    db = CutHistoryDB(tmp_path / "cut_history.db")
    result = sync_logo_folder(db, folder)
    assert result.imported == []
    assert dict(result.skipped) == {
        "logo.lgd": SKIP_NO_SERVICE_ID, "SID2-1.lgd": SKIP_NO_IMAGE_SIZE, "SID3-1.lgd": SKIP_UNREADABLE,
        "SID4-1.lgd": SKIP_NO_EDGES, "SID5-1.lgd": SKIP_NO_EDGES,
    }
    assert db.list_imported_logos() == []
    assert sync_logo_folder(db, folder).skipped == []  # 変わるまで読み直さない


def test_logos_for_videos_larger_than_1920x1080_are_not_imported(tmp_path):
    # 型を作るメモリは .lgd に書かれた映像の大きさで決まる(7680×4320 で約 1.6 GB)。録画は最大
    # 1920×1080 なので、それより大きいものは取り込まない(2026-10-22)。ファイルも読む前に大きさを見る
    folder = _folder(tmp_path)
    # ちょうど上限。BOX は 1280×720 の右上なので、1920×1080 の右上の隅に置き直す
    x, y, w, h, t = BOX
    write_lgd(folder / "SID1-1.lgd", 1920 - (WIDTH - x), y, _ring_alpha(w, h, t, 500), image_size=(1920, 1080))
    _box_lgd(folder / "SID2-1.lgd", image_size=(1921, 1080))
    _box_lgd(folder / "SID3-1.lgd", image_size=(1920, 1081))
    _box_lgd(folder / "SID4-1.lgd", image_size=(2**31 - 1, 2**31 - 1))
    with open(folder / "SID5-1.lgd", "wb") as huge:
        huge.truncate(MAX_FILE_SIZE + 1)
    db = CutHistoryDB(tmp_path / "cut_history.db")
    result = sync_logo_folder(db, folder)
    assert result.imported == ["SID1-1.lgd"]
    assert dict(result.skipped) == {name: SKIP_TOO_LARGE for name in
                                    ("SID2-1.lgd", "SID3-1.lgd", "SID4-1.lgd", "SID5-1.lgd")}
    assert MAX_FILE_SIZE > 1920 * 1080 * 12  # 映像全体を覆うロゴでも入る


def test_a_logo_whose_template_fails_is_not_read_again(tmp_path, monkeypatch):
    # 型を作れなかった(メモリ不足等)ファイルは、録画を開くたびに作り直さない
    import ts_smartcut.autocut.lgd_import as lgd_import

    calls = []

    def fail(logo):
        calls.append(logo)
        raise MemoryError

    monkeypatch.setattr(lgd_import, "template_from_lgd", fail)
    folder = _folder(tmp_path)
    _box_lgd(folder / "SID1-1.lgd")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    assert sync_logo_folder(db, folder).skipped == [("SID1-1.lgd", SKIP_UNREADABLE)]
    assert sync_logo_folder(db, folder).skipped == []
    assert len(calls) == 1


def _station_template(db, sid, x, y):
    """録画から作った局の型の代わり: 枠を x, y に描いた .lgd の型を局の型として保存する。"""
    from ts_smartcut.autocut.logo import template_to_blob

    logo = read_lgd(write_lgd(db.db_path.parent / f"station{sid}.bin", x, y, _ring_alpha(140, 70, 10, 500)))[0]
    db.save_station_logo(4, sid, template_to_blob(template_from_lgd(logo)), 2, "2026-10-01", "2026-10-08")


def test_an_imported_logo_that_does_not_match_the_station_is_reported_once(tmp_path):
    folder = _folder(tmp_path)
    _box_lgd(folder / "SID211-1.lgd")
    _box_lgd(folder / "SID141-1.lgd")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    sync_logo_folder(db, folder)
    assert check_imported_logo(db, 4, 211) is None  # 比べる型(局の型・カットした録画)が無い

    _station_template(db, 211, 1110, 60)  # ロゴの位置が変わった局
    _station_template(db, 141, 1080, 40)  # 同じロゴの局
    stale = check_imported_logo(db, 4, 211)
    assert stale is not None and stale.file_name == "SID211-1.lgd" and stale.agreement < 0.5
    assert check_imported_logo(db, 4, 141) is None
    db.mark_imported_logo_stale_notified("SID211-1.lgd")
    assert check_imported_logo(db, 4, 211) is None  # 1 回だけ


def test_logo_agreement_is_zero_when_the_station_logo_is_in_another_corner(tmp_path):
    from ts_smartcut.autocut.lgd_import import logo_agreement

    imported = template_from_lgd(read_lgd(_box_lgd(tmp_path / "a.lgd"))[0])
    other = template_from_lgd(read_lgd(write_lgd(tmp_path / "b.lgd", 60, 620, _ring_alpha(140, 70, 10, 500)))[0])
    assert logo_agreement(imported, imported) == pytest.approx(1.0)
    assert logo_agreement(imported, other) == 0.0


def test_proposals_use_the_imported_logo_only_without_a_station_template(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import ts_smartcut.autocut.features as features
    from ts_smartcut.autocut import proposals

    folder = _folder(tmp_path)
    _box_lgd(folder / "SID211-1.lgd")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    sync_logo_folder(db, folder)
    used = []

    class _Stop(Exception):
        pass

    def fake_compute(*args, station_template=None, **kwargs):
        used.append(station_template)
        raise _Stop()

    monkeypatch.setattr(features, "compute_features", fake_compute)
    info = SimpleNamespace(original_network_id=4, service_id=211)
    with pytest.raises(_Stop):
        proposals.build_file_proposals(db, tmp_path / "x.ts", FPS, info)
    assert used[-1] is not None and used[-1].has_logo == [False, True, False, False]

    station = object()
    monkeypatch.setattr("ts_smartcut.autocut.logo.load_station_logo", lambda *a, **k: station)
    with pytest.raises(_Stop):
        proposals.build_file_proposals(db, tmp_path / "x.ts", FPS, info)
    assert used[-1] is station


def test_window_opens_the_logo_folder_and_reports_a_stale_logo_once(window, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.autocut.lgd_import import StaleLogo, logo_dir

    opened, shown = [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    window._on_open_logo_folder()
    assert opened and Path(opened[0]) == logo_dir() and logo_dir().is_dir()

    if window.history_db is None:
        pytest.skip("no history DB")
    _box_lgd(logo_dir() / "SID211-1.lgd")
    sync_logo_folder(window.history_db)
    window._on_stale_logo(window._program_info_request, StaleLogo("SID211-1.lgd", "2026-01-02", 0.12))
    assert len(shown) == 1 and "SID211-1.lgd" in shown[0] and "12%" in shown[0]
    assert window.history_db.get_imported_logo(211)["stale_notified"]
    window._on_stale_logo(window._program_info_request - 1, StaleLogo("SID211-1.lgd", "", 0.1))  # 前の録画の分
    assert len(shown) == 1
    (logo_dir() / "SID211-1.lgd").unlink()
    sync_logo_folder(window.history_db)
