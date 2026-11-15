"""CM の境目の候補と区間 (設計書 §31.5、自動カット計画 A5)。

境目の候補は「無音」と「場面の切り替わり」が重なる位置。実際のカット 20 本
(2026-10-09)では、CM と本編・CM と CM の境目には必ず 1 秒前後のデジタル無音
(−100dB)があり、残す区間の始まりは場面の切り替わりのフレームちょうどだった。

- 無音: 音量(10ms ごと)が silence_db 未満の所が min_silence 秒以上続く所。
- 境目のフレーム: 無音の前後 6 フレームを含む範囲で、場面の切り替わりが最も
  強いフレーム(そのフレームから新しい場面)。long_silence 秒以上の長い無音(番組の終わりの
  数秒〜数十秒の無音、音の無い番宣カードが並ぶ所)では、scene_min 以上の山(前後のフレーム
  より強い)を、強い順に min_gap 秒以上離して全部(最も強い1つだけにすると本当の境目を
  落とした。短い無音でも全部にすると 0.1 秒の区間ができ、CM の長さが崩れた。20本で確認)。
  切り替わりが無い時は無音の中ほど。
- 区間: 境目と境目の間(録画の先頭と末尾も区切り)。min_segment_seconds 秒より短い区間は手前につなぐ。

計算は Rust の部品(native/src/rules.rs。2026-10-28 までは Python のこのファイル)。ここは結果の形と呼び出しだけ。
値(silence_db 等)は thresholds.Thresholds(局ごとに記録 DB から。無ければ初期値)。
フレーム番号はカット処理と同じ(映像の先頭が 0)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import as_packed
from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, SPECS, Thresholds


@dataclass
class Boundary:
    frame: int  # 新しい区間の最初のフレーム
    silence: float  # 無音の長さ(秒)
    scene: float  # 場面の切り替わりの強さ(0 = 見つからない)


@dataclass
class Segment:
    start: int  # [start, end)
    end: int
    fps: float

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / self.fps


def threshold_values(thresholds: Thresholds) -> List[float]:
    """Rust の部品に渡すしきい値(thresholds.SPECS の順)。"""
    return [float(getattr(thresholds, spec.name)) for spec in SPECS]


def recording(features):
    """特徴の Rust 側の写し(ts_smartcut_native.RuleRecording)。録画ごとに一度だけ作る(自動補正では同じ録画で
    判定を何百回も繰り返す)。特徴の配列を置き換えたら作り直す。音量は保存形式(uint8)、場面の切り替わりは
    float16 のまま渡す。"""
    key = (id(features.logo_score), id(features.logo_template), id(features.audio_level), id(features.scene_change),
           features.frame_count)
    cached = features.__dict__.get("_rule_recording")
    if cached is not None and cached[0] == key:
        return cached[1]
    template = features.logo_template
    corners = [i for i, yes in enumerate(template.has_logo) if yes] if template is not None else []
    if features.logo_score.shape[1] == 0:
        corners = []
    score = as_packed(features.logo_score, "u1")
    logo = b"".join(score[i].data for i in corners) if corners else None
    native = native_module().RuleRecording(
        float(features.fps), int(features.frame_count), float(features.audio_rate), float(features.audio_offset),
        as_packed(features.audio_level, "u1").data, as_packed(features.scene_change, "f2").data, logo, len(corners))
    features.__dict__["_rule_recording"] = (key, native)
    return native


def find_boundaries(features, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> List[Boundary]:
    """境目の候補(フレームの順)。"""
    return [Boundary(frame, silence, scene) for frame, silence, scene in
            native_module().rule_boundaries(recording(features), threshold_values(thresholds))]


def segments_from_boundaries(features, boundaries: Sequence[Boundary],
                             thresholds: Thresholds = DEFAULT_THRESHOLDS) -> List[Segment]:
    """境目で区切り(録画全体を覆う)、min_segment_seconds 秒より短い区間を手前につないだ区間(CM なら「CM 本体＋
    後ろの黒い画面・無音」で 15 秒の倍数に揃う。最初と最後の区間はつながない)。"""
    raw = [(b.frame, b.silence, b.scene) for b in boundaries]
    return [Segment(start, end, features.fps) for start, end in
            native_module().rule_segments(recording(features), raw, threshold_values(thresholds))]


def nearest_multiple_error(seconds: float, unit: float = 15.0) -> float:
    """長さと、いちばん近い unit 秒の倍数(unit 以上)との差(秒)。理由の文(「15 秒×N」と書くか)と学習(A8)の
    区間の特徴に使う。判定に使うのは Rust の同じ式(rules.rs)。"""
    k = max(round(seconds / unit), 1)
    return abs(seconds - k * unit)
