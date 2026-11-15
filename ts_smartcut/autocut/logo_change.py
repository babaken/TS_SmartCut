"""局のロゴが変わったかの検知 (設計書 §31.9「ロゴの変化を知らせる条件」、自動カット計画 A9)。

局の型は、その局の録画ごとの型をまとめたもの。ロゴが変わると、新しい録画の型が局の型と合わなくなる。
その局でカットした最新の録画 CHANGE_RECORDINGS 本が、それより前の録画から作った局の型と、どれも
CHANGE_AGREEMENT 未満しか合わなければ「変わった可能性がある」とする。合い方は録画の型から見た一致と
局の型から見た一致 (logo.logo_agreement) の大きい方: 録画の型には番組ごとの表示 (隅のマーク・4:3 の
番組の左右の帯の縁) も入り、録画の型から見ると、その分だけ低くなる (本物の記録で GR-C・CS-B・CS-D が
0.3 前後になった)。ロゴが変わる・動くと、どちらから見ても低い (GR-B の前のロゴで 0.00)。本物の記録
(20 局) を 1 本ずつ足していった時に「変わった」と出た局は無かった (2026-10-15)。

設計書の初めの案は「残した区間のロゴ一致度を局の型で測った平均が 0.25 未満」だったが、カットした
録画の一致度はその録画自身の型で測っていて、局の型で測った値は残らない (四隅の一時ファイルは計算の
後に消す) ので、型どうしを比べる (2026-10-15)。

「いいえ」と答えた時は、その時の合わなかった最初と最新の録画を覚え、それより後に CHANGE_RECORDINGS 本
たまるまで聞かない。聞き直す時は、合わなかった最初の録画より前の録画から比べる型を作る (ロゴが変わった
後の録画が増えると、局の型は新旧の録画が混ざって、2/3 の録画で合う画素が無くなり、ロゴが消える)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ts_smartcut.autocut.logo import combine_templates, recording_template_from_features, two_way_agreement

logger = logging.getLogger(__name__)

# 続けて合わない録画の数と、合わないとみなす一致の割合
CHANGE_RECORDINGS = 3
CHANGE_AGREEMENT = 0.5


@dataclass
class LogoChange:
    """局のロゴが変わった可能性 (画面で「作り直しますか?」と聞く)。"""

    original_network_id: Optional[int]
    service_id: int
    since: str  # 合わなかった最初の録画の日時 (作り直しの起点)
    latest: str  # 合わなかった最新の録画の日時 (「いいえ」の時に覚える)
    recording_ids: List[int]
    agreements: List[float]


def _recordings_with_logo(db, original_network_id: Optional[int], service_id: int):
    """今の型の世代の録画 (日時の順) のうち、録画の型にロゴがあるもの: (行, 型) の一覧。"""
    current = db.get_station_logo(original_network_id, service_id)
    since = current.get("generation_start") if current else None
    items = []
    for row in db.get_station_feature_blobs(original_network_id, service_id, since=since):
        template = recording_template_from_features(row["data"])
        if template is not None and template.frames > 0 and any(template.has_logo):
            items.append((row, template))
    return items


def detect_logo_change(db, original_network_id: Optional[int], service_id: int) -> Optional[LogoChange]:
    """最新の CHANGE_RECORDINGS 本が、それより前の録画の局の型と合わなければ LogoChange。"""
    items = _recordings_with_logo(db, original_network_id, service_id)
    if len(items) < CHANGE_RECORDINGS + 2:  # 比べる局の型に 2 本要る
        return None
    recent, before = items[-CHANGE_RECORDINGS:], items[:-CHANGE_RECORDINGS]
    dismissed = db.get_logo_change_dismissal(original_network_id, service_id)
    if dismissed is not None:
        dismissed_since, dismissed_through = dismissed
        if recent[0][0]["recorded_at"] <= dismissed_through:
            return None
        before = [(row, t) for row, t in before if row["recorded_at"] < dismissed_since]
    reference = combine_templates([t for _, t in before])
    if reference is None or not any(reference.has_logo):
        return None
    agreements = [two_way_agreement(t, reference) for _, t in recent]
    if any(a is None or a >= CHANGE_AGREEMENT for a in agreements):
        return None
    logger.warning("The logo of service %s may have changed: the last %d recordings agree %s with the station template",
                   service_id, CHANGE_RECORDINGS, ", ".join(f"{a:.2f}" for a in agreements))
    return LogoChange(original_network_id, service_id, recent[0][0]["recorded_at"], recent[-1][0]["recorded_at"],
                      [row["recording_id"] for row, _ in recent], agreements)


@dataclass
class StationRecording:
    """局の録画 1 本 (ロゴを作り直す時に、新しいロゴの最初の録画を選ぶ一覧)。"""

    recording_id: int
    recorded_at: str
    program_name: str
    agreement: Optional[float]  # 最新の録画との一致 (どちらかの録画にロゴが無ければ None)


def station_recordings(db, original_network_id: Optional[int], service_id: int) -> List[StationRecording]:
    """局のカットした録画 (今の世代に限らない、日時の新しい順) と、最新の録画 (ロゴのあるもの) との一致。
    新しいロゴの録画は高く、前のロゴの録画は低いので、新しいロゴの最初の録画を選びやすい (局の型は
    新旧が混ざるとロゴが消えるので、比べる相手にしない)。"""
    rows = db.get_station_feature_blobs(original_network_id, service_id)
    templates = [recording_template_from_features(row["data"]) for row in rows]
    usable = [t is not None and t.frames > 0 and any(t.has_logo) for t in templates]
    latest = next((t for t, ok in zip(reversed(templates), reversed(usable)) if ok), None)
    out = []
    for row, template, ok in zip(rows, templates, usable):
        agreement = two_way_agreement(template, latest) if ok and latest is not None else None
        recording = db.get_recording(row["recording_id"]) or {}
        out.append(StationRecording(row["recording_id"], row["recorded_at"], recording.get("program_name") or "",
                                    agreement))
    return list(reversed(out))
