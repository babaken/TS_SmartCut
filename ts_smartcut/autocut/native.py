"""Rust で書いた部品 (native/ の ts_smartcut_native) の読み込み (設計書 §31.9)。

特徴の計算の読み込み (features.py)、しきい値の自動補正の評価 (tuning.py)、番組情報の読み込み
(broadcast/program_info.py)、ルールの判定 (rules.py ほか)、読み終わった後の特徴の計算と音声波形のピーク
(features.py・logo.py・logo_image.py・core/waveform.py)、局の型のまとめ方・型どうしの比べ方・.lgd の型・ロゴ画像の
材料からロゴを求める計算 (logo.py・lgd_import.py・logo_image.py)、学習 (model.py) は Rust だけで行う (2026-10-28 利用者の決定。それまでの Python の同じ処理は削った)。
モジュールが無い・版が合わない時は E506 で、それらはできない (カットの編集・変換はできる)。`pip install ./native` で入れる。
"""

from __future__ import annotations

from typing import Optional

from ts_smartcut.autocut.thresholds import SPECS
from ts_smartcut.errors import SystemResourceError

# 使う ts_smartcut_native の版。Rust の計算の方法を変えたら native/src/lib.rs の VERSION と一緒に上げる
# (3: 番組情報の読み込み read_program_info_raw・decode_arib_text を足した。4: ルールの判定 rules.rs。5: 読み終わった後の
# 特徴の計算 post.rs。どれも 2026-10-28。6: ロゴの型どうしの計算と .lgd の型・ロゴを求める計算 logo.rs、2026-10-30。
# 7: 学習のモデル gbdt.rs (LightGBM の代わり)、2026-10-30。8: フレームごとの一致度の並べ替え frame_scores (numpy を
# やめる)、2026-10-30)
NATIVE_VERSION = 10


class NativeModuleError(SystemResourceError):
    """E506 - ts_smartcut_native が無い・版が合わない。"""


def native_module():
    """ts_smartcut_native。無い・版やしきい値の項目が合わない時は NativeModuleError。"""
    try:
        import ts_smartcut_native
    except ImportError as e:
        raise NativeModuleError("E506", key="exceptions.native_missing", error=e)
    version = getattr(ts_smartcut_native, "VERSION", None)
    if version != NATIVE_VERSION:
        raise NativeModuleError("E506", key="exceptions.native_version", found=version, expected=NATIVE_VERSION)
    if tuple(getattr(ts_smartcut_native, "THRESHOLD_NAMES", ())) != tuple(s.name for s in SPECS):
        raise NativeModuleError("E506", key="exceptions.native_thresholds")
    return ts_smartcut_native


def native_problem() -> Optional[NativeModuleError]:
    """使えない理由 (起動時の知らせ・システム情報用)。使えれば None。"""
    try:
        native_module()
    except NativeModuleError as e:
        return e
    return None
