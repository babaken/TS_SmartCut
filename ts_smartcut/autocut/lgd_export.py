"""局ロゴ画像 (.lgd) の書き出し (設計書 §31.9「局のロゴの書き出し」、A9 の後。2026-10-30)。

録画ごとに集めたロゴ画像の材料 (logo_image.py、記録 DB の logo_samples) を局ごとに足し、画素ごとの
不透明度とロゴの色を求めて (logo_image.solve)、.lgd で書く。エンコードソフトのロゴ消し・ロゴの検出、
AviUtl のロゴ消し・logoframe で使えるようにするためで、このアプリの判定には使わない (判定は今の型)。

- 局の値は局の型と同じ条件で足す: 今のロゴの世代 (ロゴを作り直した局は起点より後) の録画で、2 本以上。
  映像の大きさ (1440×1080 と 1920×1080 など)・隅ごとに 1 ファイル。位置・大きさは元の解像度で書く。
- 書き出す隅と範囲は**局の型**で決める (2026-10-30 の確かめで、録画ごとの材料の隅・範囲をそのまま使うと、
  ロゴの無い隅や、隅のほぼ全体を囲む範囲を書き出した。利用者の決定で次の 1〜4):
  1. 局の型でロゴのある隅だけ。
  2. 範囲は局の型のマスクから録画ごとと同じ決め方 (logo_image.logo_boxes) で決め、求めたロゴをその範囲で切り出す。
     範囲の幅か高さが隅の MAX_BOX_FRACTION 以上なら書き出さない (番組の枠や帯を拾った型)。
  3. 局の型が 4 隅ともロゴありの局 (型が信用できない) は、取り込んだ .lgd (エンコードソフトが作ったもの) の隅
     だけ。取り込んだ .lgd が無ければ書き出さない。
  4. ほぼ不透明で暗い画素 (不透明度 DARK_ALPHA 以上・輝度 DARK_LUMA 未満) がロゴの画素の MAX_DARK_FRACTION
     以上の隅は書き出さない (黒い帯・動かない暗い背景をロゴとして求めたもの)。
  5. 足す前に録画ごとの材料を局の範囲で 1 本ずつ求めて比べ、合わない録画を除く (Rust の部品の
     logo_sample_checks): 平らな背景のフレームが MIN_PIXEL_FRAMES より少ない録画、4 と同じ意味で暗く不透明な録画、
     ほかの録画と不透明度の相関が MIN_AGREEMENT 未満の録画 (3 本以上なら画素ごとの中央値と、2 本なら互いに比べる)。
     残りが 2 本に満たなければ書き出さない。録画の型が番組の枠などを拾って材料の範囲が広くなった録画は、背景を
     測る輪がロゴから離れて値がずれ、足すとロゴ消しで消し残りが出た (2026-10-30 の確かめで 2 局。利用者の決定で
     この PR で直す)。
- そのうえで、平らな背景のフレームが MIN_PIXEL_FRAMES 以上集まった画素が範囲の MIN_COVERED 以上あり、
  ロゴの画素 (不透明度 LOGO_ALPHA 超) が MIN_LOGO_PIXELS 以上ある隅を書き出す。書き出さなかった隅と理由は
  station_logo_report が返す (tools/check_lgd_export.py が出す)。
- 形式はエンコードソフトが読む形 (標準の ver0.1 の後に独自の拡張。拡張に映像の大きさ・サービス ID・画素ごとの
  係数が入る)。エンコードソフトは拡張を読み、拡張の無い .lgd は読めない (2026-10-30 にソースで確かめた)。
  バイト列は Rust の部品 (native/src/logo.rs の lgd_file)。このアプリの取り込み (lgd_import.py) も読める。
- ファイル名はエンコードソフトのロゴのフォルダと同じ「SID<サービス ID>-<番号>.lgd」(番号は 1 から、空いている
  もの)。既にあるファイルは上書きしない。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ts_smartcut.autocut.logo import MIN_STATION_RECORDINGS, LogoTemplate, load_station_logo
from ts_smartcut.autocut.logo_image import (
    SAMPLES_VERSION,
    CornerGeometry,
    CornerSamples,
    LogoSamples,
    SolvedLogo,
    corner_geometry,
    f8_bytes,
    logo_boxes,
    solve,
    solve_inputs,
)
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import Packed, as_packed

logger = logging.getLogger(__name__)

# 画素ごとの平らな背景のフレーム数の下限 (これより少ない画素はロゴの無い画素として書く)
MIN_PIXEL_FRAMES = 30
# 書き出す隅: 範囲の Y の画素のうち MIN_PIXEL_FRAMES 以上集まった画素の割合の下限
MIN_COVERED = 0.5
# ロゴの画素とみなす不透明度と、その画素の数の下限 (ロゴの無い隅・材料の揺れだけの隅は書かない)
LOGO_ALPHA = 0.1
MIN_LOGO_PIXELS = 16
# この不透明度以下の画素はロゴの無い画素として書く (solve はこの値以下の画素の色を求めない (0) ので、
# そのまま書くと黒に重ねることになる)
MIN_ALPHA = 0.02
# 名前の長さ (cp932 のバイト数、最後の 0 を除く): 標準の部分と拡張
NAME_BYTES = 31
EXTENDED_NAME_BYTES = 254
# 1 局・1 フォルダで探すファイル名の番号の上限
MAX_FILE_NUMBER = 1000
# 2: 局の型から決めた範囲の幅か高さが、隅の幅・高さのこの割合以上なら書き出さない
MAX_BOX_FRACTION = 0.75
# 4: ほぼ不透明で暗い画素 (帯・暗い背景) と、それがロゴの画素に占める割合の上限
DARK_ALPHA = 0.8
DARK_LUMA = 48.0
MAX_DARK_FRACTION = 0.5
# 5: 録画ごとの不透明度の、ほかの録画 (の中央値) との相関の下限 (本物の材料で、合う録画は 0.94 以上、
# 値がずれる録画は 0.69 以下だった)
MIN_AGREEMENT = 0.9
# logo_sample_checks の値
_SAMPLE_OK, _SAMPLE_FEW_FRAMES, _SAMPLE_DARK_OPAQUE, _SAMPLE_DISAGREES = 0, 1, 2, 3
# 特徴の計算の縮小 (features.py の SCALE_DIVISOR・CORNER_DIVISOR と同じ)。局の型の大きさから四隅の位置を求める
_SCALE = 4
_CORNER = 4

# 書き出さなかった理由 (tools/check_lgd_export.py が出す)
SKIP_NO_STATION_TEMPLATE = "no_station_template"  # 局の型が無い (録画が 2 本に満たない・作り直し中)
SKIP_NOT_A_LOGO_CORNER = "not_a_logo_corner"  # 局の型でロゴの無い隅
SKIP_EVERY_CORNER = "every_corner"  # 局の型が 4 隅ともロゴありで、取り込んだ .lgd の隅でない (または無い)
SKIP_OTHER_SIZE = "other_size"  # 局の型と映像の大きさが違う
SKIP_FEW_RECORDINGS = "few_recordings"  # 材料のある録画が 2 本に満たない
SKIP_TOO_LARGE = "too_large"  # 範囲が隅の大部分
SKIP_FEW_SAMPLES = "few_samples"  # 平らな背景のフレーム・ロゴの画素が足りない
SKIP_DARK_OPAQUE = "dark_opaque"  # ほぼ不透明で暗い (帯・背景)
SKIP_DISAGREE = "recordings_disagree"  # 録画ごとの材料が合わず、合う録画が 2 本に満たない


@dataclass
class StationLogoImage:
    """局のロゴ画像の 1 つの隅 (映像の大きさごと)。"""

    frame_size: Tuple[int, int]
    corner: int
    solved: SolvedLogo
    recordings: int  # 足した録画の数


@dataclass
class SkippedCorner:
    """書き出さなかった隅 (材料のあった隅だけ)。"""

    frame_size: Tuple[int, int]
    corner: int
    reason: str  # SKIP_*
    recordings: int


def station_logo_images(db, original_network_id: Optional[int], service_id: int) -> List[StationLogoImage]:
    """局の書き出せるロゴ (映像の大きさ・隅の順)。"""
    return station_logo_report(db, original_network_id, service_id)[0]


def station_logo_report(db, original_network_id: Optional[int],
                        service_id: int) -> Tuple[List[StationLogoImage], List[SkippedCorner]]:
    """局の今のロゴの世代の材料を、映像の大きさ・隅ごとに足して求めたロゴ (書き出せるもの) と、書き出さなかった隅。"""
    current = db.get_station_logo(original_network_id, service_id)
    since = current.get("generation_start") if current else None
    groups: Dict[Tuple[Tuple[int, int], int], List[CornerSamples]] = defaultdict(list)
    for row in db.get_station_logo_samples(original_network_id, service_id):
        if since is not None and row["recorded_at"] < since:
            continue
        if row["samples_version"] != SAMPLES_VERSION:
            continue
        try:
            samples = LogoSamples.from_blob(row["data"])
        except (ValueError, KeyError, OSError):
            logger.warning("Could not read the logo image samples of recording %s", row["recording_id"], exc_info=True)
            continue
        for corner in samples.corners:
            groups[(tuple(samples.frame_size), corner.corner)].append(corner)
    station = load_station_logo(db, original_network_id, service_id)
    allowed, untrusted = set(), False
    if station is not None:
        allowed = {i for i, yes in enumerate(station.has_logo) if yes}
        if len(allowed) == 4:  # 3: 型が信用できない。取り込んだ .lgd の隅だけ
            untrusted = True
            imported = _imported_corner(db, service_id)
            allowed = {imported} if imported is not None else set()
    images, skipped = [], []
    for (size, corner), items in sorted(groups.items()):
        reason, image = _corner_image(station, allowed, untrusted, size, corner, items)
        if image is None:
            logger.info("Station %s: not exporting corner %d of %dx%d (%d recordings): %s", service_id, corner,
                        size[0], size[1], len(items), reason)
            skipped.append(SkippedCorner(size, corner, reason, len(items)))
        else:
            images.append(image)
    return images, skipped


def _corner_image(station: Optional[LogoTemplate], allowed, untrusted: bool, size: Tuple[int, int], corner: int,
                  items: List[CornerSamples]) -> Tuple[Optional[str], Optional[StationLogoImage]]:
    """1 つの隅を書き出せるか: (書き出さない理由, ロゴ)。"""
    if station is None:
        return SKIP_NO_STATION_TEMPLATE, None
    if corner not in allowed:
        return (SKIP_EVERY_CORNER if untrusted else SKIP_NOT_A_LOGO_CORNER), None
    if len(items) < MIN_STATION_RECORDINGS:
        return SKIP_FEW_RECORDINGS, None
    geometry = _geometry(size, station)
    if geometry is None:
        return SKIP_OTHER_SIZE, None
    boxes = {c: (x0, y0, x1, y1) for c, x0, y0, x1, y1 in logo_boxes(station, geometry)}
    if corner not in boxes:
        return SKIP_NOT_A_LOGO_CORNER, None
    x0, y0, x1, y1 = boxes[corner]
    if x1 - x0 >= MAX_BOX_FRACTION * geometry.width or y1 - y0 >= MAX_BOX_FRACTION * geometry.height:
        return SKIP_TOO_LARGE, None
    ox, oy = geometry.origin(corner)
    area = (ox + x0, oy + y0, x1 - x0, y1 - y0)
    checks = sample_checks(items, area)
    kept = [s for s, check in zip(items, checks) if check == _SAMPLE_OK]
    if len(kept) < len(items):
        logger.info("Corner %d of %dx%d: leaving out %d of %d recordings (too few flat frames %d, dark and opaque %d, "
                    "disagreeing with the others %d)", corner, size[0], size[1], len(items) - len(kept), len(items),
                    checks.count(_SAMPLE_FEW_FRAMES), checks.count(_SAMPLE_DARK_OPAQUE), checks.count(_SAMPLE_DISAGREES))
    if len(kept) < MIN_STATION_RECORDINGS:
        if _SAMPLE_DISAGREES in checks:
            return SKIP_DISAGREE, None
        if _SAMPLE_DARK_OPAQUE in checks:
            return SKIP_DARK_OPAQUE, None
        return SKIP_FEW_SAMPLES, None
    solved = solve(kept)
    cropped = crop(solved, area) if solved is not None else None
    if cropped is None or not exportable(cropped):
        return SKIP_FEW_SAMPLES, None
    if dark_opaque(cropped):
        return SKIP_DARK_OPAQUE, None
    return None, StationLogoImage(size, corner, cropped, len(kept))


def sample_checks(items: List[CornerSamples], area: Tuple[int, int, int, int]) -> List[int]:
    """録画ごとの材料を局の範囲 area (フレームの座標) で比べた結果 (修正 5、_SAMPLE_*。Rust の部品)。"""
    return list(native_module().logo_sample_checks(
        solve_inputs(items), tuple(int(v) for v in area), MIN_PIXEL_FRAMES, LOGO_ALPHA, DARK_ALPHA, DARK_LUMA,
        MAX_DARK_FRACTION, MIN_AGREEMENT))


def _geometry(size: Tuple[int, int], station: LogoTemplate) -> Optional[CornerGeometry]:
    """映像の大きさの、元の解像度の四隅 (特徴の計算と同じ)。局の型の大きさと合わなければ None。"""
    w, h = -(-size[0] // _SCALE), -(-size[1] // _SCALE)
    cw, ch = w // _CORNER, h // _CORNER
    if (ch // 2, cw // 2) != (station.gx.shape[1], station.gy.shape[2]):
        return None
    return corner_geometry(size[0], size[1], (cw, ch), _SCALE)


def _imported_corner(db, service_id: int) -> Optional[int]:
    """局の取り込んだ .lgd (いちばん新しいもの) のロゴの隅 (ロゴの中心がある隅)。無い・読めなければ None。"""
    from ts_smartcut.autocut.lgd_import import LgdFormatError, parse_lgd

    row = db.get_imported_logo(service_id)
    if row is None:
        return None
    try:
        logos = parse_lgd(bytes(row["lgd_data"]), row["file_name"])
    except (LgdFormatError, ValueError):
        return None
    if len(logos) != 1:
        return None
    logo = logos[0]
    width, height = row["image_width"], row["image_height"]
    right = logo.x + logo.width / 2 >= width / 2
    bottom = logo.y + logo.height / 2 >= height / 2
    return (2 if bottom else 0) + (1 if right else 0)


def crop(solved: SolvedLogo, box: Tuple[int, int, int, int]) -> Optional[SolvedLogo]:
    """求めたロゴを、フレームの座標の範囲 box (x, y, 幅, 高さ。偶数) との重なりで切り出す。重ならなければ None。"""
    sx, sy, sw, sh = solved.box
    x0, y0 = max(sx, box[0]), max(sy, box[1])
    x1, y1 = min(sx + sw, box[0] + box[2]), min(sy + sh, box[1] + box[3])
    if x1 <= x0 or y1 <= y0:
        return None
    planes = []
    for values in (solved.alpha, solved.color, solved.frames):
        cut = []
        for p, plane in enumerate(values):
            d = 1 if p == 0 else 2
            cut.append(_crop_plane(as_packed(plane, "f8"), (y0 - sy) // d, (y1 - sy) // d, (x0 - sx) // d,
                                   (x1 - sx) // d))
        planes.append(cut)
    return SolvedLogo((x0, y0, x1 - x0, y1 - y0), *planes)


def _crop_plane(plane: Packed, r0: int, r1: int, c0: int, c1: int) -> Packed:
    """2 次元の値の行 r0〜r1・列 c0〜c1 (終わりは含まない)。"""
    size, width = plane.itemsize, plane.shape[1]
    data = b"".join(plane.data[(r * width + c0) * size:(r * width + c1) * size] for r in range(r0, r1))
    return Packed(data, plane.dtype, (r1 - r0, c1 - c0))


def dark_opaque(solved: SolvedLogo) -> bool:
    """ロゴの画素の MAX_DARK_FRACTION 以上が、ほぼ不透明で暗い画素か (帯・暗い背景をロゴとして求めたもの)。"""
    count = dark = 0
    for a, c, n in zip(*(as_packed(v[0], "f8").flat() for v in (solved.alpha, solved.color, solved.frames))):
        if a > LOGO_ALPHA and n >= MIN_PIXEL_FRAMES:
            count += 1
            dark += a >= DARK_ALPHA and c < DARK_LUMA
    return count > 0 and dark >= MAX_DARK_FRACTION * count


def exportable(solved: SolvedLogo) -> bool:
    """平らな背景のフレームが十分に集まり、ロゴの画素がある隅か。"""
    covered = [n >= MIN_PIXEL_FRAMES for n in solved.frames[0].flat()]
    if not covered or sum(covered) / len(covered) < MIN_COVERED:
        return False
    return sum(1 for a, ok in zip(solved.alpha[0].flat(), covered) if ok and a > LOGO_ALPHA) >= MIN_LOGO_PIXELS


def cp932_name(name: str, limit: int) -> bytes:
    """名前の cp932 のバイト列 (limit バイトまで。文字の途中では切らない)。"""
    data = b""
    for ch in name:
        piece = ch.encode("cp932", errors="replace")
        if len(data) + len(piece) > limit:
            break
        data += piece
    return data


def lgd_bytes(name: str, solved: SolvedLogo, service_id: int, frame_size: Tuple[int, int]) -> bytes:
    """ロゴ 1 つの .lgd (エンコードソフトの形)。"""
    x, y, w, h = solved.box

    def planes(values):
        return [f8_bytes(v) for v in values]

    return native_module().lgd_file(
        cp932_name(name, NAME_BYTES), cp932_name(name, EXTENDED_NAME_BYTES), int(service_id),
        int(frame_size[0]), int(frame_size[1]), int(x), int(y), int(w), int(h),
        planes(solved.alpha), planes(solved.color), planes(solved.frames), float(MIN_PIXEL_FRAMES), MIN_ALPHA)


def _write_new(folder: Path, service_id: int, data: bytes) -> Path:
    """folder/SID<サービス ID>-<番号>.lgd の空いている名前 (番号は 1 から) に書く。既にあるファイルは上書きしない。"""
    for n in range(1, MAX_FILE_NUMBER + 1):
        path = folder / f"SID{service_id}-{n}.lgd"
        try:
            with open(path, "xb") as f:
                f.write(data)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free logo file name for service {service_id} in {folder}")


def export_station_lgd(db, original_network_id: Optional[int], service_id: int, station_name: str,
                       folder: Path, date: Optional[str] = None) -> List[Path]:
    """局のロゴを folder に .lgd で書き出す (映像の大きさ・隅ごとに 1 ファイル)。書いたファイルの一覧
    (書き出せるロゴが無ければ空)。ロゴの名前は「局名(日付)」(日付は足した最新の録画の日。ロゴが 2 つ以上の
    隅にある時は「局名 #隅の番号(日付)」)。"""
    images = station_logo_images(db, original_network_id, service_id)
    if not images:
        return []
    if date is None:
        rows = db.get_station_logo_samples(original_network_id, service_id)
        date = rows[-1]["recorded_at"][:10] if rows and rows[-1]["recorded_at"] else None
    corners_per_size: Dict[Tuple[int, int], int] = defaultdict(int)
    for image in images:
        corners_per_size[image.frame_size] += 1
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for image in images:
        name = station_name if corners_per_size[image.frame_size] == 1 else f"{station_name} #{image.corner + 1}"
        data = lgd_bytes(f"{name}({date})" if date else name, image.solved, service_id, image.frame_size)
        path = _write_new(folder, service_id, data)
        logger.info("Exported the station logo of service %s (%dx%d, corner %d, %d recordings) to %s", service_id,
                    image.frame_size[0], image.frame_size[1], image.corner, image.recordings, path)
        written.append(path)
    return written
