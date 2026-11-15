"""ルールのしきい値 (設計書 §31.9「ルールのしきい値」、自動カット計画「A8 の後の作業」1)。

境目の候補(candidates.py)とルール(rules.py)の判定に使う値をまとめる。値は記録 DB(版 8 の
thresholds)に局ごと・全局共通で持ち、

    局の値 → 全局共通の値 → 初期値(ここに書いた値)

の順に、行のある最初のものを使う。DB の行は、手で直した時(source=manual)か自動補正
(tuning.py)の結果を採用した時(source=auto)にだけ作る(初めから写さない。全局共通の値を
変えた時、手を入れていない局にもそのまま効くように)。

初期値は 2026-10-09 に実際のカット 20 本で決めた値。
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Thresholds:
    # ---- 境目の候補(candidates.py) ----
    # 無音とみなす音量(dB)と長さ(秒)(20本の境目 129 か所で、−80dB・0.5 秒で 3 フレーム以内に
    # 候補があったのは 122 か所。外れたのは長い無音の中の境目と、黒からのフェードイン)
    silence_db: float = -80.0
    min_silence: float = 0.5
    # 境目をいくつも探す長い無音(秒)
    long_silence: float = 2.0
    # 場面の切り替わりとみなす強さ(前のフレームとの差の平均、0〜255)
    scene_min: float = 15.0
    # 長い無音の中の境目の最小の間隔(秒)
    min_gap: float = 1.0
    # ---- ロゴ(rules.py) ----
    logo_program: float = 0.25  # これ以上なら本編(OP でロゴが 0.33 の局があった)
    logo_cm: float = 0.2  # これ以下なら CM の候補(CM でも 0.16〜0.18 の局があった)
    # ---- 長さ(rules.py) ----
    length_tolerance: float = 0.5  # 5 秒の倍数とみなす差(秒)。局の番宣は 5・10・20 秒もある
    max_cm_seconds: float = 120.0  # 1つの区間(CM 1本)の長さの上限
    # 長い番宣(CS-A の 180 秒等): CM が続く所で 15 秒の倍数にぴったり(±long_tolerance 秒)なら
    # long_cm_seconds 秒まで CM とみなす(本編がたまたま当てはまる見込みを小さくするため厳しく)
    long_tolerance: float = 0.15
    long_cm_seconds: float = 300.0
    # 本編と CM・番組の前後の間の短い区間とみなす長さ
    short_seconds: float = 20.5
    # これより短い区間は次の区間につなげる(CM の間の 1〜2 秒の黒い画面等で、CM の長さが崩れた)
    min_segment_seconds: float = 2.0
    long_program_seconds: float = 150.0  # これより長い区間は CM 1本ではない
    # 区間のうち無音(silence_db 未満)の割合がこれ以上なら「音の無い区間」。CS-A の番組の後には、
    # 音の無い番宣カード(3〜43 秒の半端な長さ)が並ぶ
    silent_fraction: float = 0.7

    def replace(self, **values) -> "Thresholds":
        return dataclasses.replace(self, **values)

    def as_dict(self) -> Dict[str, float]:
        return dataclasses.asdict(self)


DEFAULT_THRESHOLDS = Thresholds()

CANDIDATES, LOGO, LENGTH = "candidates", "logo", "length"


@dataclass(frozen=True)
class ThresholdSpec:
    """値ごとの範囲(手入力の範囲チェックと、自動補正で試す値)。"""

    name: str
    group: str
    minimum: float
    maximum: float
    step: float  # 手入力の刻み・自動補正で試す間隔
    decimals: int

    @property
    def default(self) -> float:
        return getattr(DEFAULT_THRESHOLDS, self.name)

    def grid(self) -> List[float]:
        """自動補正で試す値(範囲を step 刻み)。"""
        count = int(round((self.maximum - self.minimum) / self.step))
        return [round(self.minimum + k * self.step, self.decimals) for k in range(count + 1)]

    def accepts(self, value: float) -> bool:
        return self.minimum - 1e-9 <= value <= self.maximum + 1e-9


# 画面に出す順。範囲は「おかしな値を受け付けない」ための幅(初期値の数倍)
SPECS: Tuple[ThresholdSpec, ...] = (
    ThresholdSpec("silence_db", CANDIDATES, -100.0, -40.0, 5.0, 0),
    ThresholdSpec("min_silence", CANDIDATES, 0.1, 2.0, 0.1, 1),
    ThresholdSpec("long_silence", CANDIDATES, 1.0, 6.0, 0.5, 1),
    ThresholdSpec("scene_min", CANDIDATES, 5.0, 50.0, 2.5, 1),
    ThresholdSpec("min_gap", CANDIDATES, 0.25, 3.0, 0.25, 2),
    ThresholdSpec("logo_program", LOGO, 0.05, 0.8, 0.05, 2),
    ThresholdSpec("logo_cm", LOGO, 0.0, 0.6, 0.05, 2),
    ThresholdSpec("length_tolerance", LENGTH, 0.1, 1.5, 0.1, 1),
    ThresholdSpec("max_cm_seconds", LENGTH, 30.0, 240.0, 15.0, 0),
    ThresholdSpec("long_tolerance", LENGTH, 0.05, 0.5, 0.05, 2),
    ThresholdSpec("long_cm_seconds", LENGTH, 120.0, 600.0, 30.0, 0),
    ThresholdSpec("short_seconds", LENGTH, 5.0, 60.0, 2.5, 1),
    ThresholdSpec("min_segment_seconds", LENGTH, 0.5, 5.0, 0.5, 1),
    ThresholdSpec("long_program_seconds", LENGTH, 60.0, 300.0, 15.0, 0),
    ThresholdSpec("silent_fraction", LENGTH, 0.3, 0.95, 0.05, 2),
)
SPEC_BY_NAME: Dict[str, ThresholdSpec] = {s.name: s for s in SPECS}


def consistency_error(t: Thresholds) -> Optional[str]:
    """値どうしの関係が崩れていれば、その組(文字の一覧の項目)。無ければ None。"""
    if t.logo_cm > t.logo_program:
        return "thresholds.error_logo_order"
    if t.long_silence < t.min_silence:
        return "thresholds.error_silence_order"
    if t.long_cm_seconds < t.max_cm_seconds:
        return "thresholds.error_cm_order"
    return None


# ---- 行(DB の値)から使う値を決める ---------------------------------------------

STATION, GLOBAL, DEFAULT = "station", "global", "default"  # 値の出どころ(段)
MANUAL, AUTO = "manual", "auto"  # 行の種類


def station_key(original_network_id: Optional[int], service_id: Optional[int]) -> str:
    """DB の行の局の鍵。全局共通は ""。"""
    if service_id is None:
        return ""
    onid = "" if original_network_id is None else str(original_network_id)
    return f"{onid}:{service_id}"


@dataclass(frozen=True)
class ThresholdRow:
    value: float
    source: str  # MANUAL / AUTO


# 局の鍵 → 項目 → 行
Rows = Mapping[str, Mapping[str, ThresholdRow]]


def _valid(name: str, row: ThresholdRow, warn: bool = True) -> bool:
    spec = SPEC_BY_NAME.get(name)
    if spec is None:
        return False  # 新しい版のアプリが足した項目等
    if not spec.accepts(row.value):
        if warn:
            logger.warning("Ignored an out-of-range threshold in the history DB: %s=%s", name, row.value)
        return False
    return True


def resolve_detail(rows: Rows, key: str, warn: bool = True) -> Dict[str, Tuple[float, str, Optional[str]]]:
    """項目 → (使う値, 段 STATION/GLOBAL/DEFAULT, 行の種類 MANUAL/AUTO/None)。"""
    station = rows.get(key, {}) if key else {}
    common = rows.get("", {})
    result = {}
    for spec in SPECS:
        row = station.get(spec.name)
        if row is not None and _valid(spec.name, row, warn):
            result[spec.name] = (row.value, STATION, row.source)
            continue
        row = common.get(spec.name)
        if row is not None and _valid(spec.name, row, warn):
            result[spec.name] = (row.value, GLOBAL, row.source)
            continue
        result[spec.name] = (spec.default, DEFAULT, None)
    return result


def resolve(rows: Rows, key: str, warn: bool = True) -> Thresholds:
    """局(key。全局共通は "")で使う値。値どうしの関係が崩れる組み合わせ(局の値と全局共通の
    値が混ざった時に起こりうる)は初期値で判定する。warn=False ならログに残さない(自動補正で
    何百回も呼ぶ時)。"""
    values = {name: v for name, (v, _level, _source) in resolve_detail(rows, key, warn).items()}
    t = DEFAULT_THRESHOLDS.replace(**values)
    if consistency_error(t) is not None:
        if warn:
            logger.warning("Thresholds for station %r are inconsistent; using the defaults", key)
        return DEFAULT_THRESHOLDS
    return t


def load_thresholds(db, original_network_id: Optional[int], service_id: Optional[int]) -> Thresholds:
    """記録 DB から、その局で使う値。DB が無ければ初期値。"""
    if db is None:
        return DEFAULT_THRESHOLDS
    return resolve(db.get_threshold_rows(), station_key(original_network_id, service_id))
