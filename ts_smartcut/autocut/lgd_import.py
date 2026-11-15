"""局ロゴ画像 (.lgd) の取り込み (設計書 §31.9、利用者の決定 2026-10-15)。

エンコードソフトが録画から作った .lgd (logoframe / delogo の形式) を読み、ロゴの型
(logo.py の LogoTemplate) を作って記録 DB に取り込む。局の型 (同じ局の録画 2 本から作る)
がまだ無い局で、カットする前の録画の一致度に使う (1 本目からロゴで案を出せる)。

取り込みは自動 (利用者の決定 2026-10-15): 設定と同じフォルダの Logo/ に .lgd をコピーしておくと、録画を
開くたびにファイルの更新日時と大きさを比べ、新しい・変わったファイルだけを取り込む (消したファイルは
DB からも消す)。取り込んだロゴが古い (カットした録画のロゴと合わない) 時は、1 ファイルにつき 1 回知らせる。

.lgd の形:
- 先頭 32 バイト: "<logo data file ver0.1>" などの文字列と、最後の 1 バイトがロゴの数。
- ロゴごとに、名前 32 バイト (cp932)、x・y・高さ・幅・フェード等 (short 8 個)、画素ごとに
  不透明度と色 (short 6 個: 輝度の不透明度 0〜1000、輝度、Cb の不透明度、Cb、Cr の不透明度、Cr)。
  輝度は 0〜4096 が 8 ビットの 16〜235。
- ロゴの後に、作った時の映像の大きさなどを入れた拡張がある (先頭が 0x12345)。映像の大きさが
  分からないと型の位置を合わせられないので、拡張の無い .lgd は取り込まない。

型の作り方: いろいろな明るさの背景にロゴを重ねた隅を、特徴の計算と同じ縮小 (1/4 → 四隅 →
1/2) で作り、録画の型と同じように縁の向きを数える。背景の明るさはロゴの明るさをまたぐように
16〜235 を均等に並べ、少しノイズを加える (平らな背景の縁は 0 で数えない)。計算は Rust の部品
(native/src/logo.rs の lgd_template。2026-10-30 までは numpy で、ノイズの乱数だけが違う)。
"""

from __future__ import annotations

import logging
import re
import struct
import threading
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ts_smartcut.autocut.logo import LogoTemplate, logo_agreement, template_from_native
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import Packed

logger = logging.getLogger(__name__)

_FILE_HEADER = 32
_NAME = 32
_LOGO_HEADER = _NAME + 16
_PIXEL = 12
_EXTENSION_MAGIC = 0x12345
# 輝度の不透明度の最大 (1000 = 不透明)
MAX_ALPHA = 1000
# 型を作る背景の数 (型のフレーム数。背景の明るさ・ノイズ・縮小は Rust の部品)
BACKGROUNDS = 64

_DATE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)\s*$")


@dataclass
class LgdLogo:
    """.lgd のロゴ 1 つ。alpha は 0〜1、luma は 8 ビット (16〜235 の目盛り、範囲外もある)。"""

    name: str
    date: Optional[str]  # 名前の末尾の (YYYY-MM-DD)
    x: int
    y: int
    width: int
    height: int
    image_width: Optional[int]  # 作った時の映像の大きさ (拡張が無ければ None)
    image_height: Optional[int]
    alpha: Packed  # float32 (高さ, 幅)
    luma: Packed  # float32 (高さ, 幅)


class LgdFormatError(ValueError):
    pass


def read_lgd(path: Path) -> List[LgdLogo]:
    """.lgd のロゴの一覧。形が違えば LgdFormatError。"""
    return parse_lgd(Path(path).read_bytes(), path)


def parse_lgd(data: bytes, path="") -> List[LgdLogo]:
    """.lgd のバイト列のロゴの一覧 (path はエラーの文に入れる名前)。形が違えば LgdFormatError。"""
    if len(data) < _FILE_HEADER or not data.startswith(b"<logo data file"):
        raise LgdFormatError(f"not a logo data file: {path}")
    count = data[_FILE_HEADER - 1]
    offset = _FILE_HEADER
    logos = []
    for _ in range(count):
        if offset + _LOGO_HEADER > len(data):
            raise LgdFormatError(f"logo header is cut off: {path}")
        name = data[offset:offset + _NAME].split(b"\0")[0].decode("cp932", errors="replace")
        x, y, h, w = struct.unpack_from("<hhhh", data, offset + _NAME)
        offset += _LOGO_HEADER
        size = w * h * _PIXEL
        if w <= 0 or h <= 0 or offset + size > len(data):
            raise LgdFormatError(f"logo pixels are cut off: {path}")
        pixels = array("h")
        pixels.frombytes(data[offset:offset + size])
        offset += size
        alpha = Packed.from_values((min(max(v / MAX_ALPHA, 0.0), 1.0) for v in pixels[0::6]), "f4", (h, w))
        luma = Packed.from_values((16.0 + v * (219.0 / 4096.0) for v in pixels[1::6]), "f4", (h, w))
        match = _DATE.search(name)
        logos.append(LgdLogo(name, match.group(1) if match else None, x, y, w, h, None, None, alpha, luma))
    # 拡張 (エンコードソフト独自): 0x12345・版・幅・高さ・…・映像の幅・高さ・x・y
    if len(logos) == 1 and offset + 40 <= len(data):
        magic, _version, w, h, _ux, _uy, image_w, image_h, x, y = struct.unpack_from("<10i", data, offset)
        logo = logos[0]
        if magic == _EXTENSION_MAGIC and (w, h, x, y) == (logo.width, logo.height, logo.x, logo.y):
            logo.image_width, logo.image_height = image_w, image_h
    return logos


def service_id_from_name(path: Path) -> Optional[int]:
    """ファイル名 SID<番号>-<n>.lgd の局の番号 (サービス ID)。"""
    match = re.match(r"SID(\d+)-\d+\.lgd$", Path(path).name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def template_from_lgd(logo: LgdLogo) -> Optional[LogoTemplate]:
    """ロゴを、その映像の大きさの録画の局の型にする。映像の大きさが分からない・ロゴが
    映像の外にはみ出す・どの隅にも縁が残らない時は None。"""
    width, height = logo.image_width, logo.image_height
    if not width or not height:
        return None
    result = native_module().lgd_template(
        logo.alpha.astype("f4").data, logo.luma.astype("f4").data,
        logo.x, logo.y, logo.width, logo.height, width, height)
    if result is None:
        return None
    h, w, raw = result
    return template_from_native(raw, (h, w), BACKGROUNDS)


# ---- 記録 DB への取り込み (Logo/ フォルダとの同期) -------------------------------

LOGO_DIR_NAME = "Logo"

# 取り込めなかった理由 (ログに書く)
SKIP_NO_SERVICE_ID = "no_service_id"  # ファイル名が SID<番号>-<n>.lgd でない
SKIP_UNREADABLE = "unreadable"  # .lgd の形でない・読めない
SKIP_NO_IMAGE_SIZE = "no_image_size"  # 映像の大きさが入っていない
SKIP_NO_EDGES = "no_edges"  # 映像の外にはみ出す・縁が残らない
SKIP_TOO_LARGE = "too_large"  # 映像・ファイルが大きすぎる

# 取り込む映像の大きさの上限 (利用者の決定 2026-10-22: 録画は最大 1920×1080)。型を作る時のメモリは
# 映像の面積に比例し (1920×1080 で約 0.1 GB、7680×4320 で約 1.6 GB)、.lgd の数値だけで決まるため
MAX_IMAGE_WIDTH = 1920
MAX_IMAGE_HEIGHT = 1080
# ファイルの大きさの上限: 映像全体を覆うロゴ 1 つ (画素ごとに 12 バイト) と見出し・拡張が入る大きさ。
# ファイルは丸ごと読むので、読む前に確かめる
MAX_FILE_SIZE = _FILE_HEADER + _LOGO_HEADER + MAX_IMAGE_WIDTH * MAX_IMAGE_HEIGHT * _PIXEL + 1024

# 取り込んだロゴが古いとみなす一致の割合 (2026-10-15 の確かめ: 今のロゴどうしは 0.84〜1.00、
# 変わる前のロゴは 0.00・0.73)
STALE_AGREEMENT = 0.5


def logo_dir() -> Path:
    """取り込む .lgd を置くフォルダ (設定・記録 DB と同じフォルダの Logo/)。"""
    from ts_smartcut.core.settings_manager import get_config_dir

    return get_config_dir() / LOGO_DIR_NAME


@dataclass
class SyncResult:
    imported: List[str]  # 取り込んだ (新しい・変わった) ファイル名
    skipped: List[tuple]  # (ファイル名, 理由)
    removed: List[str]  # フォルダから消えたので DB から消したファイル名


# 取り込めなかったファイル ((フォルダ, 名前) → (更新日時, 大きさ))。変わるまで読み直さない
_skipped_files: dict = {}
_sync_lock = threading.Lock()


def _import_file(db, path: Path, mtime_ns: int, size: int) -> Optional[str]:
    """1 ファイルを取り込む。取り込めなければその理由。"""
    from datetime import datetime

    from ts_smartcut.autocut.logo import template_to_blob

    service_id = service_id_from_name(path)
    if service_id is None:
        return SKIP_NO_SERVICE_ID
    if size > MAX_FILE_SIZE:
        return SKIP_TOO_LARGE
    try:
        raw = path.read_bytes()
        logos = read_lgd(path)
    except (OSError, LgdFormatError, ValueError, struct.error):
        logger.warning("Could not read the logo file: %s", path, exc_info=True)
        return SKIP_UNREADABLE
    if len(logos) != 1:
        return SKIP_UNREADABLE
    logo = logos[0]
    if not logo.image_width or not logo.image_height:
        return SKIP_NO_IMAGE_SIZE
    if logo.image_width > MAX_IMAGE_WIDTH or logo.image_height > MAX_IMAGE_HEIGHT:
        return SKIP_TOO_LARGE
    try:
        template = template_from_lgd(logo)
    except (ValueError, MemoryError):
        # 取り込めなかったファイルとして覚え、変わるまで読み直さない (録画を開くたびにやり直さない)
        logger.warning("Could not make a template from the logo file: %s", path, exc_info=True)
        return SKIP_UNREADABLE
    if template is None:
        return SKIP_NO_EDGES
    date = logo.date or datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d")
    db.save_imported_logo(service_id, path.name, logo.name, date, logo.image_width, logo.image_height,
                          template_to_blob(template), raw, mtime_ns, size)
    return None


def sync_logo_folder(db, folder: Optional[Path] = None) -> SyncResult:
    """フォルダ (既定は logo_dir()) の .lgd と記録 DB の取り込んだロゴを合わせる。更新日時と大きさが
    DB と同じファイルは読まない (録画を開くたびに呼ぶので、変わらなければ数ミリ秒)。"""
    folder = Path(folder) if folder is not None else logo_dir()
    result = SyncResult([], [], [])
    with _sync_lock:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            files = {p.name: p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".lgd"}
        except OSError:
            logger.warning("Could not read the logo folder: %s", folder, exc_info=True)
            return result
        known = {row["file_name"]: row for row in db.list_imported_logos()}
        for name in sorted(files, key=str.lower):
            path = files[name]
            try:
                stat = path.stat()
            except OSError:
                continue
            version = (stat.st_mtime_ns, stat.st_size)
            row = known.get(name)
            if row is not None and (row["file_mtime_ns"], row["file_size"]) == version:
                continue
            key = (str(folder), name)
            if _skipped_files.get(key) == version:
                continue
            reason = _import_file(db, path, *version)
            if reason is None:
                _skipped_files.pop(key, None)
                result.imported.append(name)
            else:
                _skipped_files[key] = version
                result.skipped.append((name, reason))
        for name in known:
            if name not in files:
                db.delete_imported_logo(name)
                result.removed.append(name)
    if result.imported or result.skipped or result.removed:
        logger.info("Logo folder %s: imported %d, skipped %d, removed %d", folder,
                    len(result.imported), len(result.skipped), len(result.removed))
        for name, reason in result.skipped:
            logger.info("Skipped logo file %s: %s", name, reason)
    return result


def load_imported_logo(db, service_id: int) -> Optional[LogoTemplate]:
    """局の取り込んだロゴのうち、いちばん新しいものの型 (無ければ None)。ファイル名に
    original_network_id が無いので、局はサービス ID だけで見分ける。"""
    from ts_smartcut.autocut.logo import template_from_blob

    row = db.get_imported_logo(service_id)
    if row is None:
        return None
    logger.info("Using the imported logo %s (%s) for service %d", row["file_name"], row["logo_date"], service_id)
    return template_from_blob(row["logo_data"])


# ---- 取り込んだロゴが古いか ------------------------------------------------------


@dataclass
class StaleLogo:
    """古いとみなした取り込んだロゴ (画面で知らせる)。"""

    file_name: str
    logo_date: Optional[str]
    agreement: float


def check_imported_logo(db, original_network_id: Optional[int], service_id: int) -> Optional[StaleLogo]:
    """局のいちばん新しい取り込んだロゴを、局の型 (無ければその局でカットした最新の録画の型) と比べ、
    合わなければ (STALE_AGREEMENT 未満) StaleLogo。知らせたことのあるファイル・比べる型が無い時は None。"""
    from ts_smartcut.autocut.logo import load_station_logo, recording_template_from_features, template_from_blob

    row = db.get_imported_logo(service_id)
    if row is None or row.get("stale_notified"):
        return None
    reference = load_station_logo(db, original_network_id, service_id)
    if reference is None:
        for feature_row in reversed(db.get_station_feature_blobs(original_network_id, service_id)):
            template = recording_template_from_features(feature_row["data"])
            if template is not None and template.frames > 0:
                reference = template
                break
    if reference is None:
        return None
    agreement = logo_agreement(template_from_blob(row["logo_data"]), reference)
    if agreement is None or agreement >= STALE_AGREEMENT:
        return None
    logger.warning("The imported logo %s (%s) does not match the recordings of service %d (agreement %.2f)",
                   row["file_name"], row["logo_date"], service_id, agreement)
    return StaleLogo(row["file_name"], row["logo_date"], agreement)
