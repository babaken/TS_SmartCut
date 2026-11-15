"""ルールによる区間の判定 (設計書 §31.3・§31.5、自動カット計画 A5)。

境目の候補(candidates.py)で区切った区間ごとに「本編か、切る区間(CM・番組の前後)か」を
決め、本編の区間をつないで「残す区間の案」を作る。案ごとに確信度(0〜1)と理由を付ける。

ルール(実際のカット 20 本で確かめた、2026-10-09):
1. 番組の前後: 区間の中ほどが EIT の放送時間(録画開始からの秒数)の外なら切る。
2. ロゴ: 局のロゴが出る録画では、ロゴの一致度の平均が logo_program 以上なら本編、
   logo_cm 以下なら CM の候補。
3. 長さ: 15 秒の倍数(±length_tolerance 秒)で max_cm_seconds 秒以下なら CM らしい。
   ロゴで決まらない区間(ロゴの出ない局 = CS-A・公共放送 等、ロゴがはっきりしない区間)は、
   CM らしい区間が並んでいる所(前後のどちらかも CM らしい・番組の前後に接している)
   だけを CM にする(本編の中にたまたま 15 秒の倍数の区間があっても切らない)。
4. 残す区間の終わり: 境目(新しい場面の最初のフレーム)から、その局の過去のカットで
   測ったフレーム数だけ手前にする(利用者は CM の直前の 1〜2 フレームを残さない)。

番組の最後の次回予告(5〜15 秒、ロゴが無いことが多い)は CM と見分けられず、切る案に
なることがある(A8 の学習で補う)。

判定(種類・確信度・理由の種類)と案の区間の組み立ては Rust の部品(native/src/rules.rs の classify・assemble。
決まりの説明もそちら。2026-10-28 までは Python のこのファイル)。ここは理由の文(言語ファイル)と結果の形。
小文字で書いた値(logo_program 等)は thresholds.Thresholds(局ごとに記録 DB から。無ければ初期値)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from ts_smartcut.autocut.candidates import (
    Boundary,
    Segment,
    find_boundaries,
    nearest_multiple_error,
    recording,
    segments_from_boundaries,
    threshold_values,
)
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, Thresholds
from ts_smartcut.i18n.language_manager import tr

DEFAULT_END_OFFSET = 1  # 残す区間の終わりを境目の何フレーム手前にするか(局の記録が無い時)

PROGRAM, CM, MARGIN = "program", "cm", "margin"


@dataclass
class ProgramWindow:
    """EIT の放送時間(映像の先頭からの秒)。"""

    start: float
    end: float


@dataclass
class StationProfile:
    """局ごとの値(過去のカットから測る。proposals.station_profile)。"""

    end_offset: int = DEFAULT_END_OFFSET
    # 本編と CM の間の短い区間(提供クレジット等)を利用者が残した割合。None は記録なし(残す)。
    # 局によって残す(BS-A・BS-B)・切る(GR-D)が分かれた
    short_between_kept: Optional[float] = None


@dataclass
class SegmentDecision:
    segment: Segment
    kind: str  # PROGRAM / CM / MARGIN
    confidence: float
    reason: str
    logo: Optional[float] = None  # ロゴの一致度の平均(ロゴの無い局は None)
    rule: str = ""  # 判定の種類(理由の文は言語で変わるので、文ではなくこれで見分ける)


# SegmentDecision.rule: 本編と CM の間の短い区間(提供・次回予告。局ごとに残すかを記録から測る)
SHORT_BETWEEN = "short_between"


@dataclass
class KeepProposal:
    """残す区間の案 [start, end)。"""

    start: int
    end: int
    confidence: float
    reason: str
    decisions: List[SegmentDecision] = field(default_factory=list)


def _format_seconds(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    if seconds >= 59.5:
        # 先に秒へ丸める(分と秒を別々に丸めると 119.6 秒が「1分60秒」になった)
        whole = int(round(seconds))
        return tr("reasons.minutes_seconds", minutes=whole // 60, seconds=f"{whole % 60:02d}")
    return tr("reasons.seconds", value=f"{seconds:.1f}" if seconds < 10 else int(round(seconds)))


def _length_text(seconds: float, t: Thresholds) -> str:
    err = nearest_multiple_error(seconds)
    if err <= t.length_tolerance:
        return tr("reasons.multiple_15", length=_format_seconds(seconds), count=max(round(seconds / 15), 1))
    return _format_seconds(seconds)


def _window(window: Optional[ProgramWindow]) -> Optional[Tuple[float, float]]:
    return None if window is None else (float(window.start), float(window.end))


def _profile(profile: Optional[StationProfile]) -> Optional[Tuple[int, Optional[float]]]:
    return None if profile is None else (int(profile.end_offset), profile.short_between_kept)


def _reason_text(reason: str, seg: Segment, logo: Optional[float], t: Thresholds) -> str:
    """Rust の判定の理由の種類(rules.rs の Reason)を文にする。"""
    length = _length_text(seg.seconds, t)
    logo_text = tr("reasons.logo_suffix", value=f"{logo:.2f}") if logo is not None else tr("reasons.no_logo_station")
    if reason in ("margin_before", "margin_after"):
        where = tr("reasons.before_start") if reason == "margin_before" else tr("reasons.after_end")
        return tr("reasons.margin", where=where)
    if reason in ("logo_yes", "logo_no"):
        return tr(f"reasons.{reason}", logo=f"{logo:.2f}", length=length)
    if reason == "long":
        return tr("reasons.long", length=length) + (tr("reasons.logo_suffix", value=f"{logo:.2f}") if logo is not None else "")
    return tr(f"reasons.{reason}", length=length, logo=logo_text)


def classify_segments(features, segments: Sequence[Segment], window: Optional[ProgramWindow],
                      profile: Optional[StationProfile] = None,
                      thresholds: Thresholds = DEFAULT_THRESHOLDS) -> List[SegmentDecision]:
    """区間ごとの判定(種類・確信度・理由。決まりは native/src/rules.rs の classify)。"""
    raw = native_module().rule_classify(recording(features), [(s.start, s.end) for s in segments], _window(window),
                                        _profile(profile), threshold_values(thresholds))
    return [SegmentDecision(seg, kind, confidence, _reason_text(reason, seg, logo, thresholds), logo,
                            SHORT_BETWEEN if short_between else "")
            for seg, (kind, confidence, reason, logo, short_between) in zip(segments, raw)]


def segment_values(features, segments: Sequence[Segment],
                   thresholds: Thresholds = DEFAULT_THRESHOLDS) -> List[Tuple[Optional[float], float]]:
    """区間ごとの (ロゴの一致度の平均(ロゴの無い局は None), 音量が silence_db 未満の割合)。学習(A8)の区間の特徴。"""
    return native_module().rule_segment_values(recording(features), [(s.start, s.end) for s in segments],
                                               threshold_values(thresholds))


def propose(features, window: Optional[ProgramWindow] = None,
            profile: Optional[StationProfile] = None,
            boundaries: Optional[List[Boundary]] = None,
            thresholds: Thresholds = DEFAULT_THRESHOLDS) -> List[KeepProposal]:
    """残す区間の案(フレームの順)。boundaries は同じ thresholds で求めたもの(省くと求める)。"""
    profile = profile or StationProfile()
    if features.frame_count == 0:
        return []
    boundaries = find_boundaries(features, thresholds) if boundaries is None else boundaries
    segments = segments_from_boundaries(features, boundaries, thresholds)
    decisions = classify_segments(features, segments, window, profile, thresholds)
    return assemble_proposals(decisions, profile)


def assemble_proposals(decisions: Sequence[SegmentDecision],
                       profile: Optional[StationProfile] = None) -> List[KeepProposal]:
    """区間ごとの判定(ルール・学習 A8)から、本編の区間をつないだ残す区間の案。区間は native/src/rules.rs の
    assemble(次が切る区間なら、境目の局の値だけ手前で終える)。確信度は、つないだ区間と前後の切る区間の最小。"""
    profile = profile or StationProfile()
    runs = native_module().rule_assemble([d.kind for d in decisions],
                                         [(d.segment.start, d.segment.end) for d in decisions],
                                         int(profile.end_offset))
    proposals: List[KeepProposal] = []
    for start, end, first, last, prev_index, next_index in runs:
        run = list(decisions[first:last])
        prev_decision = decisions[prev_index] if prev_index is not None else None
        next_decision = decisions[next_index] if next_index is not None else None
        confidences = [d.confidence for d in run]
        confidences += [d.confidence for d in (prev_decision, next_decision) if d is not None]
        main = max(run, key=lambda d: d.segment.seconds)
        parts = [tr("reasons.program", length=_format_seconds(sum(d.segment.seconds for d in run)), reason=main.reason)]
        if prev_decision is not None:
            parts.append(tr("reasons.before", reason=prev_decision.reason))
        if next_decision is not None:
            parts.append(tr("reasons.after", reason=next_decision.reason))
        proposals.append(KeepProposal(start, end, min(confidences), " / ".join(parts), run))
    return proposals
