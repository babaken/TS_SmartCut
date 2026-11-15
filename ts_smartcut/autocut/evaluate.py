"""提案と正解(記録したカット)の一致率 (設計書 §31.7、自動カット計画 A7)。

録画ごとに、ルール(A5)の「残す区間の案」と、利用者が実際に残した区間を比べる。
「基準外」の区間はどちらにも数えない。

- 切る時間(CM・番組の前後)について:
  - 再現率 = 正しく切った時間 / 本当に切った時間(CM の時間のうち当てた割合)
  - 適合率 = 正しく切った時間 / 案で切る時間
  - 本編を切った時間: 案では切るが、利用者は残した時間(取り戻せない誤り。少ないほどよい)
- 境目: 正解の境目(残す区間の始まり・終わり)ごとに、いちばん近い案の境目との差(フレーム)。
  ±0、±2 以内、±1 秒以内、1 秒より離れている(外れ)に分けて数える。
- 局ごと・地上波/BS/CS ごとに合計する。

ルールの値は、評価する録画自身を除いた過去のカットから測る(proposals.station_profile)。
しきい値は記録 DB の局の値(thresholds.py)。defaults=True なら初期値(比べるため)。
ただし、ロゴの一致度は記録した時に「その録画自身の残した区間」で作った型で計算して
あるので、その分だけ実際(カットする前は局の型を使う)より良く出る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ts_smartcut.autocut.features import RecordingFeatures
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.proposals import keep_ranges, recording_proposals
from ts_smartcut.autocut.rules import KeepProposal
from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, Thresholds

EXACT, NEAR, SECOND, MISS = "±0", "±2f", "±1秒", "外れ"


@dataclass
class Score:
    """切る時間(秒)と境目の数。足し合わせられる。"""

    truth_cut: float = 0.0  # 本当に切った時間
    proposed_cut: float = 0.0  # 案で切る時間
    correct_cut: float = 0.0  # 正しく切った時間
    wrong_cut: float = 0.0  # 本編を切った時間
    edges: Dict[str, int] = field(default_factory=lambda: {EXACT: 0, NEAR: 0, SECOND: 0, MISS: 0})
    extra_edges: int = 0  # 正解に無い案の境目(±1秒以内に正解の境目が無い)
    recordings: int = 0

    def add(self, other: "Score") -> None:
        self.truth_cut += other.truth_cut
        self.proposed_cut += other.proposed_cut
        self.correct_cut += other.correct_cut
        self.wrong_cut += other.wrong_cut
        for k, v in other.edges.items():
            self.edges[k] += v
        self.extra_edges += other.extra_edges
        self.recordings += other.recordings

    @property
    def recall(self) -> float:
        return self.correct_cut / self.truth_cut if self.truth_cut else 1.0

    @property
    def precision(self) -> float:
        return self.correct_cut / self.proposed_cut if self.proposed_cut else 1.0

    @property
    def edge_count(self) -> int:
        return sum(self.edges.values())


@dataclass
class RecordingResult:
    recording_id: int
    station: str
    broadcast_type: str
    program: str
    score: Score
    proposals: List[KeepProposal]
    truth: List[Tuple[int, int, bool]]


def compare(truth: Sequence[Tuple[int, int, bool]], proposals: Sequence[KeepProposal],
            frame_count: int, fps: float) -> Score:
    """案と正解の比較(native/src/rules.rs の compare。切る時間は「基準外」の外で数え、境目は正解の境目ごとに
    いちばん近い案の境目との差と、±1 秒以内に正解の境目が無い案の境目の数)。"""
    values = native_module().rule_compare([(int(a), int(b), bool(ex)) for a, b, ex in truth],
                                          [(int(p.start), int(p.end)) for p in proposals], int(frame_count), float(fps))
    truth_cut, proposed_cut, correct_cut, wrong_cut, exact, near, second, miss, extra, recordings = values
    return Score(truth_cut, proposed_cut, correct_cut, wrong_cut, {EXACT: exact, NEAR: near, SECOND: second, MISS: miss},
                 extra, recordings)


def evaluate_recording(db, recording: dict, features: RecordingFeatures,
                       thresholds: Optional[Thresholds] = None) -> RecordingResult:
    truth = keep_ranges(db, recording["id"])
    proposals = recording_proposals(db, recording, features, exclude_self=True, thresholds=thresholds)
    score = compare(truth, proposals, features.frame_count, features.fps)
    return RecordingResult(
        recording["id"], recording.get("service_name") or "?", recording.get("broadcast_type") or "?",
        recording.get("program_name") or "", score, proposals, truth,
    )


def evaluate_all(db, recording_ids: Optional[Sequence[int]] = None,
                 defaults: bool = False) -> List[RecordingResult]:
    """記録 DB の録画(最後まで計算した特徴と記録したカットがあるもの)をすべて評価する。defaults なら
    記録 DB のしきい値を使わず初期値で。"""
    with db.get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT r.*, f.data AS feature_data FROM recordings r
            JOIN features f ON f.recording_id = r.id
            WHERE f.feature_version >= 2 AND f.is_complete = 1 ORDER BY r.id;
            """
        )]
    results = []
    for row in rows:
        if recording_ids is not None and row["id"] not in recording_ids:
            continue
        features = RecordingFeatures.from_blob(row.pop("feature_data"))
        if not keep_ranges(db, row["id"]):
            continue
        results.append(evaluate_recording(db, row, features, DEFAULT_THRESHOLDS if defaults else None))
    return results


def summarize(results: Sequence[RecordingResult], key) -> Dict[str, Score]:
    """key(result) ごとに合計する。"""
    groups: Dict[str, Score] = {}
    for r in results:
        groups.setdefault(key(r), Score()).add(r.score)
    return groups
