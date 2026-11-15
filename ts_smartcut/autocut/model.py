"""学習による区間の判定 (設計書 §31.5・§31.9、自動カット計画 A8)。

ルール(rules.py)と同じ境目の候補・区間(candidates.py、局のしきい値)を使い、区間ごとに「切るか」を
勾配ブースティング決定木で判定する(2026-10-26 から LightGBM、2026-10-30 から Rust の部品の GbdtModel
(native/src/gbdt.rs。LightGBM と同じ考え方・同じ設定。.exe を小さくするため、段階 ④))。局は特徴の 1 つにした 1 つのモデル(データの少ない局でも
他局の傾向を使う)。残す区間の組み立て・区間の終わりの差(局の傾向)はルールと同じ(rules.assemble_proposals)。

- 区間の特徴(FEATURE_NAMES): 長さ・15 / 5 秒の倍数とのずれ・ロゴの一致度・音の無い割合・前後の境目の
  無音の長さと場面の切り替わり・番組の放送時間と録画の端からの位置・前後の区間の長さとロゴ・ルールの判定
  (本区間と前後)・局・地上波/BS/CS・ジャンル・字幕と音声の形式の変化。
- 正解: 区間のうち利用者が残した割合が半分未満なら「切る」。「基準外」の区間に掛かる区間は使わない。
  特徴が途中まで(is_complete = 0)の録画は使わない(2026-10-15 の決定)。
- 重み: 区間の長さ(秒、1〜MAX_WEIGHT_SECONDS)。長い区間を誤る方が、切る時間・本編を切る時間とも大きいため。
- 切る閾値: 学習に使う録画を録画ごとに FOLDS 個に分けた予測(その録画を学習に使わないモデルの予測)で、
  良さ(正しく切った秒 − THRESHOLD_WRONG_CUT_WEIGHT × 本編を切った秒)が最大になる値。本編を切った秒を
  しきい値の補正(WRONG_CUT_WEIGHT = 10)の倍に見る: 2026-10-26 の確かめ(記録 DB の写し、学習 236 本・
  確認 78 本)で、重みなし・10 倍は本編を切った時間がルールの 136 秒に対して 215 秒、長さの重み・20 倍は 169 秒
  (再現率 77.5% → 95.2%)。
- 確かめ(train_and_evaluate): 新しい録画 EVAL_FRACTION を確認用にし(録画の日付で分ける、計画書 A7)、
  残りで学習したモデルの案とルールの案を確認用の録画で比べる。ルールより良い(良さが上がり、再現率が
  MAX_RECALL_DROP より下がらない)時だけ、すべての録画で学習し直したモデルを使う印(is_active)を付ける。

ロゴの一致度は、記録した後に計算した特徴では「その録画自身の残した区間」で作った型で出してあるので、
学習と確かめでは実際(カットする前は局の型)より少し良く出る(評価 A7 と同じ)。

学習には Rust の部品が要る(無い時は E506 の知らせが出て、ルールで提案する)。LightGBM で学習した前のモデル
(library が "lightgbm")は使わない(学習し直すまでルールで提案する)。
"""

from __future__ import annotations

import json
import logging
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ts_smartcut.autocut.candidates import (
    Boundary,
    Segment,
    find_boundaries,
    nearest_multiple_error,
    segments_from_boundaries,
)
from ts_smartcut.autocut.evaluate import Score, compare
from ts_smartcut.autocut.features import RecordingFeatures
from ts_smartcut.autocut.native import native_module, native_problem
from ts_smartcut.autocut.packed import Packed
from ts_smartcut.autocut.proposals import item_measures, keep_ranges, profile_from_measures, program_window
from ts_smartcut.autocut.rules import (
    CM,
    MARGIN,
    PROGRAM,
    SHORT_BETWEEN,
    KeepProposal,
    ProgramWindow,
    SegmentDecision,
    StationProfile,
    _format_seconds,
    assemble_proposals,
    classify_segments,
    segment_values,
)
from ts_smartcut.autocut.thresholds import Thresholds, load_thresholds, station_key
from ts_smartcut.autocut.tuning import MAX_RECALL_DROP, WRONG_CUT_WEIGHT, objective
from ts_smartcut.i18n.language_manager import tr

logger = logging.getLogger(__name__)

LIBRARY = "native-gbdt"
# 区間の特徴の版。特徴を変えたら上げる(保存したモデルと合わなければ使わない)
MODEL_FEATURE_VERSION = 1
EVAL_FRACTION = 0.25  # 確認用にする新しい録画の割合
FOLDS = 5  # 切る閾値を決める時の分け方
SEED = 1
_KINDS = {PROGRAM: 0, CM: 1, MARGIN: 2}
_BROADCAST_TYPES = {"terrestrial": 0, "bs": 1, "cs": 2}
_CUT_THRESHOLDS = [round(0.30 + 0.05 * k, 2) for k in range(14)] + [0.97, 0.98, 0.99]  # 0.30〜0.95 と 0.97〜0.99
THRESHOLD_WRONG_CUT_WEIGHT = 2 * WRONG_CUT_WEIGHT
MAX_WEIGHT_SECONDS = 120.0
# 学習の設定 (2026-10-26 に LightGBM で決めた値のまま。二値の分類)
_PARAMS = {
    "rounds": 300,
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 20,
    "lambda_l2": 1.0,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "seed": SEED,
}

FEATURE_NAMES = [
    "seconds", "err15", "err5", "logo", "has_logo", "silent_fraction",
    "silence_before", "silence_after", "scene_before", "scene_after",
    "from_program_start", "to_program_end", "position", "from_recording_start", "to_recording_end",
    "prev_seconds", "next_seconds", "prev_logo", "next_logo", "prev_err15", "next_err15",
    "rule_kind", "rule_confidence", "rule_short_between", "prev_rule_kind", "next_rule_kind",
    "station", "broadcast_type", "genre",
    "caption_rate", "caption_change", "audio_channels", "audio_change", "pmt_change",
]
CATEGORICAL = ["rule_kind", "prev_rule_kind", "next_rule_kind", "station", "broadcast_type", "genre"]


class LearningCancelled(Exception):
    pass


def learning_available() -> bool:
    """学習できるか (Rust の部品が使えるか)。"""
    return native_problem() is None


@dataclass
class LearnItem:
    """学習・確かめに使う録画 1 本。"""

    recording_id: int
    station_key: str
    broadcast_type: Optional[str]
    genre: Optional[int]
    recorded_at: str
    features: RecordingFeatures
    truth: List[Tuple[int, int, bool]]
    window: Optional[ProgramWindow]
    thresholds: Thresholds


@dataclass
class SegmentTable:
    """録画 1 本の区間ごとの特徴(行列)と、ルールの判定。"""

    segments: List[Segment]
    boundaries: List[Optional[Boundary]]  # 区間の始まりの境目(先頭は None)
    rule_decisions: List[SegmentDecision]
    x: Packed  # float64 (区間の数, len(FEATURE_NAMES))、分からない値は NaN


# ---- 区間の特徴 -------------------------------------------------------------------


def _err(seconds: float, unit: float) -> float:
    return nearest_multiple_error(seconds, unit)


def _changes_near(times: Sequence[float], t: float, within: float = 1.0) -> float:
    return float(any(abs(x - t) <= within for x in times))


def segment_table(features: RecordingFeatures, window: Optional[ProgramWindow], profile: StationProfile,
                  thresholds: Thresholds, station_code: float, broadcast_type: Optional[str],
                  genre: Optional[int], boundaries: Optional[List[Boundary]] = None) -> SegmentTable:
    fps = features.fps
    if boundaries is None:
        boundaries = find_boundaries(features, thresholds)
    segments = segments_from_boundaries(features, boundaries, thresholds)
    decisions = classify_segments(features, segments, window, profile, thresholds)
    by_frame = {b.frame: b for b in boundaries}
    starts = [by_frame.get(s.start) for s in segments]
    total = features.frame_count / fps if fps else 0.0
    has_logo = features.logo_template is not None and any(features.logo_template.has_logo)
    values = segment_values(features, segments, thresholds)  # (ロゴの一致度の平均, 無音の割合)
    logos = [logo for logo, _silent in values]
    captions = [float(t) for t in features.streams.caption_times]
    audio = features.streams.audio_channels
    audio_times = [float(t) for t, _c in audio[1:]]
    pmt_times = [float(t) for t, _s in features.streams.pmt_changes[1:]]
    nan = float("nan")
    rows = []
    n = len(segments)
    for i, seg in enumerate(segments):
        a, b = seg.start / fps, seg.end / fps
        middle = (a + b) / 2
        d = decisions[i]
        prev_d = decisions[i - 1] if i > 0 else None
        next_d = decisions[i + 1] if i + 1 < n else None
        prev_s = segments[i - 1] if i > 0 else None
        next_s = segments[i + 1] if i + 1 < n else None
        before = starts[i]
        after = starts[i + 1] if i + 1 < n else None
        rate_here = sum(1 for t in captions if a <= t < b) / max(b - a, 1e-3)
        rate_around = sum(1 for t in captions if a - 30 <= t < b + 30) / max(b - a + 60, 1e-3)
        channels = nan
        for t, c in audio:
            if t <= middle:
                channels = float(c)
        rows.append([
            seg.seconds, _err(seg.seconds, 15.0), _err(seg.seconds, 5.0),
            nan if logos[i] is None else logos[i], float(has_logo), values[i][1],
            before.silence if before else nan, after.silence if after else nan,
            before.scene if before else nan, after.scene if after else nan,
            middle - window.start if window else nan, window.end - middle if window else nan,
            middle / total if total else nan, a, total - b,
            prev_s.seconds if prev_s else nan, next_s.seconds if next_s else nan,
            nan if (prev_s is None or logos[i - 1] is None) else logos[i - 1],
            nan if (next_s is None or logos[i + 1] is None) else logos[i + 1],
            _err(prev_s.seconds, 15.0) if prev_s else nan, _err(next_s.seconds, 15.0) if next_s else nan,
            _KINDS[d.kind], d.confidence, float(d.rule == SHORT_BETWEEN),
            _KINDS[prev_d.kind] if prev_d else nan, _KINDS[next_d.kind] if next_d else nan,
            station_code, _BROADCAST_TYPES.get(broadcast_type or "", nan),
            float(genre >> 4) if genre is not None else nan,
            rate_here, rate_here - rate_around,
            channels, max(_changes_near(audio_times, a), _changes_near(audio_times, b)),
            max(_changes_near(pmt_times, a), _changes_near(pmt_times, b)),
        ])
    x = Packed.from_values((v for row in rows for v in row), "f8", (n, len(FEATURE_NAMES)))
    return SegmentTable(segments, starts, decisions, x)


def segment_labels(table: SegmentTable, truth: Sequence[Tuple[int, int, bool]], fps: float):
    """区間ごとの (切るか, 切った秒, 残した秒, 使うか) のリスト。基準外に掛かる区間は使わない。"""
    n = len(table.segments)
    cut, cut_s, keep_s, use = [False] * n, [0.0] * n, [0.0] * n, [True] * n
    for i, seg in enumerate(table.segments):
        kept = sum(max(0, min(b, seg.end) - max(a, seg.start)) for a, b, ex in truth if not ex)
        if any(ex and a < seg.end and seg.start < b for a, b, ex in truth):
            use[i] = False
        keep_s[i] = kept / fps
        cut_s[i] = (seg.end - seg.start - kept) / fps
        cut[i] = kept < (seg.end - seg.start) / 2
    return cut, cut_s, keep_s, use


# ---- 録画の読み込み -------------------------------------------------------------------


def load_items(db, stop_event=None, progress: Optional[Callable[[int, int], None]] = None) -> List[LearnItem]:
    """学習に使う録画(最後まで計算した特徴と有効なカットがあるもの)。古い順。"""
    with db.get_connection() as conn:
        records = [dict(r) for r in conn.execute(
            """
            SELECT r.id, r.original_network_id, r.service_id, r.broadcast_type, r.genre,
                   r.recording_start, r.broadcast_start, r.broadcast_duration, r.created_at
            FROM recordings r JOIN features f ON f.recording_id = r.id
            WHERE f.feature_version >= 2 AND f.is_complete = 1 AND r.service_id IS NOT NULL;
            """
        )]
    rows_cache: Dict[str, Thresholds] = {}
    items: List[LearnItem] = []
    for k, r in enumerate(records):
        if stop_event is not None and stop_event.is_set():
            raise LearningCancelled()
        if progress is not None:
            progress(k, len(records))
        truth = keep_ranges(db, r["id"])
        if not truth:
            continue
        row = db.get_features_row(r["id"])
        try:
            features = RecordingFeatures.from_blob(row["data"])
        except Exception:  # noqa: BLE001
            logger.warning("Could not read features: recording_id=%s", r["id"], exc_info=True)
            continue
        key = station_key(r["original_network_id"], r["service_id"])
        if key not in rows_cache:
            rows_cache[key] = load_thresholds(db, r["original_network_id"], r["service_id"])
        items.append(LearnItem(
            r["id"], key, r["broadcast_type"], r["genre"],
            str(r["recording_start"] or r["broadcast_start"] or r["created_at"] or ""),
            features, truth,
            program_window(r["recording_start"], r["broadcast_start"], r["broadcast_duration"]),
            rows_cache[key],
        ))
    items.sort(key=lambda it: (it.recorded_at, it.recording_id))
    return items


def station_profiles(items: Sequence[LearnItem]) -> Dict[int, StationProfile]:
    """録画ごとの局の傾向(その録画を除いた同じ局の録画から。評価 A7 と同じ)。"""
    by_station: Dict[str, List[LearnItem]] = {}
    for it in items:
        by_station.setdefault(it.station_key, []).append(it)
    result: Dict[int, StationProfile] = {}
    for station_items in by_station.values():
        t = station_items[0].thresholds
        measures = item_measures([(it.recording_id, it.features, it.truth, it.window) for it in station_items], t)
        for it in station_items:
            result[it.recording_id] = profile_from_measures(measures, exclude_recording=it.recording_id)
    return result


# ---- モデル -------------------------------------------------------------------


@dataclass
class LearnedModel:
    booster: object  # ts_smartcut_native.GbdtModel
    station_codes: Dict[str, int]
    cut_threshold: float
    feature_version: int = MODEL_FEATURE_VERSION

    def station_code(self, key: str) -> float:
        code = self.station_codes.get(key)
        return float(code) if code is not None else float("nan")

    def predict_cut(self, x: Packed) -> List[float]:
        """区間ごとの切る確率。x は float64 (区間の数, len(FEATURE_NAMES))。"""
        return _predict(self.booster, x)

    def to_blob(self) -> bytes:
        payload = {
            "model": self.booster.to_text(),
            "station_codes": self.station_codes,
            "cut_threshold": self.cut_threshold,
            "feature_version": self.feature_version,
            "feature_names": FEATURE_NAMES,
        }
        return zlib.compress(json.dumps(payload).encode("utf-8"))

    @classmethod
    def from_blob(cls, blob: bytes) -> "LearnedModel":
        """保存したモデル。LightGBM のモデル(2026-10-30 より前)・壊れたものは ValueError。"""
        payload = json.loads(zlib.decompress(blob).decode("utf-8"))
        if "model" not in payload:
            raise ValueError("not a model of this app's learning (made with an earlier library)")
        return cls(native_module().GbdtModel.from_text(payload["model"]), dict(payload["station_codes"]),
                   float(payload["cut_threshold"]), int(payload["feature_version"]))


def _take(x: Packed, rows: Sequence[int]) -> Packed:
    """行列 x (行, 列) の rows の行だけ。"""
    width = x.shape[1] * x.itemsize
    return Packed(b"".join(x.data[r * width:(r + 1) * width] for r in rows), x.dtype, (len(rows), x.shape[1]))


def _fit(x: Packed, y: Sequence[bool], weight: Sequence[float]):
    return native_module().GbdtModel.train(
        x.astype("f8").data, x.shape[0], len(FEATURE_NAMES), Packed.from_values((float(v) for v in y), "f8").data,
        Packed.from_values(weight, "f8").data, [FEATURE_NAMES.index(c) for c in CATEGORICAL], **_PARAMS)


def _predict(model, x: Packed) -> List[float]:
    if x.shape[0] == 0:
        return []
    return Packed(model.predict(x.astype("f8").data, x.shape[0], x.shape[1]), "f8").flat()


@dataclass
class _Rows:
    """学習に使う区間をまとめたもの。"""

    x: Packed  # float64 (区間の数, len(FEATURE_NAMES))
    y: List[bool]
    cut_s: List[float]
    keep_s: List[float]
    recording: List[int]

    @property
    def weight(self) -> List[float]:
        return [min(max(c + k, 1.0), MAX_WEIGHT_SECONDS) for c, k in zip(self.cut_s, self.keep_s)]


def _rows(tables: Dict[int, SegmentTable], items: Sequence[LearnItem]) -> _Rows:
    data, ys, cs, ks, rs = [], [], [], [], []
    width = len(FEATURE_NAMES)
    for it in items:
        table = tables[it.recording_id]
        y, cut_s, keep_s, use = segment_labels(table, it.truth, it.features.fps)
        chosen = [i for i, ok in enumerate(use) if ok]
        data.append(_take(table.x, chosen).data)
        ys += [y[i] for i in chosen]
        cs += [cut_s[i] for i in chosen]
        ks += [keep_s[i] for i in chosen]
        rs += [it.recording_id] * len(chosen)
    return _Rows(Packed(b"".join(data), "f8", (len(ys), width)), ys, cs, ks, rs)


def _choose_cut_threshold(rows: _Rows, stop_event=None) -> float:
    """録画ごとに FOLDS 個に分けた予測で、良さ(正しく切った秒 − 重み × 本編を切った秒)が最大の閾値。"""
    recordings = sorted(set(rows.recording))
    if len(recordings) < 2:
        return 0.5
    fold_of = {r: k % FOLDS for k, r in enumerate(recordings)}
    fold = [fold_of[r] for r in rows.recording]
    predicted = [0.0] * len(rows.y)
    weight = rows.weight
    for k in range(min(FOLDS, len(recordings))):
        if stop_event is not None and stop_event.is_set():
            raise LearningCancelled()
        test = [i for i, f in enumerate(fold) if f == k]
        train = [i for i, f in enumerate(fold) if f != k]
        if not test or not train:
            continue
        booster = _fit(_take(rows.x, train), [rows.y[i] for i in train], [weight[i] for i in train])
        for i, p in zip(test, _predict(booster, _take(rows.x, test))):
            predicted[i] = p
    best, best_value = 0.5, float("-inf")
    for threshold in _CUT_THRESHOLDS:
        cut = [i for i, p in enumerate(predicted) if p >= threshold]
        value = sum(rows.cut_s[i] for i in cut) - THRESHOLD_WRONG_CUT_WEIGHT * sum(rows.keep_s[i] for i in cut)
        if value > best_value:
            best, best_value = float(threshold), value
    return best


def train_model(tables: Dict[int, SegmentTable], items: Sequence[LearnItem], station_codes: Dict[str, int],
                stop_event=None) -> LearnedModel:
    rows = _rows(tables, items)
    threshold = _choose_cut_threshold(rows, stop_event)
    if stop_event is not None and stop_event.is_set():
        raise LearningCancelled()
    return LearnedModel(_fit(rows.x, rows.y, rows.weight), dict(station_codes), threshold)


# ---- 案 -------------------------------------------------------------------


def learned_decisions(model: LearnedModel, table: SegmentTable) -> List[SegmentDecision]:
    """区間ごとの学習の判定。ルールで番組の前後とした区間を切るなら、種類は番組の前後のまま。"""
    p_cut = model.predict_cut(table.x)
    result = []
    for seg, rule, p in zip(table.segments, table.rule_decisions, p_cut):
        length = _format_seconds(seg.seconds)
        if p >= model.cut_threshold:
            kind = MARGIN if rule.kind == MARGIN else CM
            result.append(SegmentDecision(seg, kind, float(p), tr("reasons.learned_cut", length=length,
                                                                    percent=int(round(p * 100))),
                                          rule.logo, rule="learned"))
        else:
            result.append(SegmentDecision(seg, PROGRAM, float(1 - p), tr("reasons.learned_program", length=length,
                                                                           percent=int(round((1 - p) * 100))),
                                          rule.logo, rule="learned"))
    return result


def learned_proposals(model: LearnedModel, features: RecordingFeatures, window: Optional[ProgramWindow],
                      profile: StationProfile, thresholds: Thresholds, key: str,
                      broadcast_type: Optional[str], genre: Optional[int]) -> List[KeepProposal]:
    if features.frame_count == 0:
        return []
    table = segment_table(features, window, profile, thresholds, model.station_code(key), broadcast_type, genre)
    return assemble_proposals(learned_decisions(model, table), profile)


# ---- 学習と確かめ -------------------------------------------------------------------


@dataclass
class StationResult:
    station_key: str
    train_recordings: int = 0
    eval_recordings: int = 0
    rule: Score = field(default_factory=Score)
    learned: Score = field(default_factory=Score)


@dataclass
class TrainingResult:
    model: Optional[LearnedModel]  # すべての録画で学習したモデル(使わない時も保存して見せる)
    train_recordings: int
    eval_recordings: int
    samples: int  # 学習に使った区間の数(すべての録画)
    rule: Score  # 確認用の録画での、ルールの評価
    learned: Score  # 確認用の録画での、学習の評価
    stations: Dict[str, StationResult]
    seconds: float = 0.0

    def adoption_problem(self) -> Optional[str]:
        """使わない理由(ログ用の英語)。使ってよければ None。"""
        if self.eval_recordings == 0:
            return "no recordings left to check the model with"
        if not objective(self.learned) > objective(self.rule):
            return (f"not better than the rules on recordings left out (score {objective(self.learned):.1f} "
                    f"vs {objective(self.rule):.1f})")
        if self.learned.recall < self.rule.recall - MAX_RECALL_DROP:
            return (f"recall on recordings left out drops from {self.rule.recall * 100:.1f}% "
                    f"to {self.learned.recall * 100:.1f}%")
        return None

    def details(self) -> dict:
        def score(s: Score) -> dict:
            return {"recall": s.recall, "precision": s.precision, "wrong_cut": s.wrong_cut,
                    "truth_cut": s.truth_cut, "correct_cut": s.correct_cut, "proposed_cut": s.proposed_cut,
                    "near_edges": s.edges.get("±0", 0) + s.edges.get("±2f", 0), "edges": s.edge_count,
                    "recordings": s.recordings}

        return {
            "rule": score(self.rule), "learned": score(self.learned),
            "cut_threshold": self.model.cut_threshold if self.model else None,
            "stations": {k: {"train": v.train_recordings, "eval": v.eval_recordings,
                             "rule": score(v.rule), "learned": score(v.learned)}
                         for k, v in self.stations.items()},
        }


def train_and_evaluate(items: Sequence[LearnItem], stop_event=None,
                       progress: Optional[Callable[[str], None]] = None) -> TrainingResult:
    """新しい録画 EVAL_FRACTION で確かめ、すべての録画で学習したモデルを返す(items は古い順)。"""
    started = datetime.now(timezone.utc)

    def check():
        if stop_event is not None and stop_event.is_set():
            raise LearningCancelled()

    def report(stage: str):
        if progress is not None:
            progress(stage)

    items = list(items)
    station_codes = {k: i for i, k in enumerate(sorted({it.station_key for it in items}))}
    report("segments")
    profiles = station_profiles(items)
    tables: Dict[int, SegmentTable] = {}
    for it in items:
        check()
        tables[it.recording_id] = segment_table(it.features, it.window, profiles[it.recording_id], it.thresholds,
                                                float(station_codes[it.station_key]), it.broadcast_type, it.genre)
    n_eval = int(round(len(items) * EVAL_FRACTION)) if len(items) >= 8 else 0
    train_items, eval_items = items[:len(items) - n_eval], items[len(items) - n_eval:]
    stations: Dict[str, StationResult] = {}
    for it in train_items:
        stations.setdefault(it.station_key, StationResult(it.station_key)).train_recordings += 1
    rule_total, learned_total = Score(), Score()
    if eval_items:
        report("evaluate")
        check()
        held_model = train_model(tables, train_items, station_codes, stop_event)
        for it in eval_items:
            check()
            table = tables[it.recording_id]
            profile = profiles[it.recording_id]
            rule_score = compare(it.truth, assemble_proposals(table.rule_decisions, profile),
                                 it.features.frame_count, it.features.fps)
            learned_score = compare(it.truth, assemble_proposals(learned_decisions(held_model, table), profile),
                                    it.features.frame_count, it.features.fps)
            st = stations.setdefault(it.station_key, StationResult(it.station_key))
            st.eval_recordings += 1
            st.rule.add(rule_score)
            st.learned.add(learned_score)
            rule_total.add(rule_score)
            learned_total.add(learned_score)
    report("train")
    check()
    model = train_model(tables, items, station_codes, stop_event)
    samples = len(_rows(tables, items).y)
    return TrainingResult(model, len(train_items), len(eval_items), samples, rule_total, learned_total, stations,
                          (datetime.now(timezone.utc) - started).total_seconds())


# ---- 学習し直す・使うモデル・方式 (A9 の残り、設計書 §31.9) ------------------------------

METHOD_DEFAULT, METHOD_RULE, METHOD_LEARNED = "default", "rule", "learned"
# 学習に使った録画がこれより少ない局は、学習を選んでいてもルールで提案する(設計書 §31.9
# 「学習のデータが足りない局」。しきい値の自動補正の MIN_TUNE_RECORDINGS と同じ 2 本)
MIN_STATION_RECORDINGS = 2

# 局で学習を使えない理由(画面の文字は gui 側で)
REASON_NO_MODEL = "no_model"  # 学習のモデルが無い・ルールより悪い・Rust の部品が無い
REASON_FEW_RECORDINGS = "few_recordings"
REASON_RULE_BETTER = "rule_better"  # 確認用の録画で、その局はルールの方が良かった


def _score_value(d: dict) -> float:
    """details に保存した評価の良さ(tuning.objective と同じ式)。"""
    from ts_smartcut.autocut.tuning import EDGE_WEIGHT

    return float(d["correct_cut"] - WRONG_CUT_WEIGHT * d["wrong_cut"] + EDGE_WEIGHT * d["near_edges"])


def station_problem(details: Optional[dict], key: str) -> Optional[str]:
    """使うモデルの details で、その局に学習を使えない理由(使えれば None)。"""
    if not details:
        return REASON_NO_MODEL
    st = details.get("stations", {}).get(key)
    if st is None or st.get("train", 0) + st.get("eval", 0) < MIN_STATION_RECORDINGS:
        return REASON_FEW_RECORDINGS
    if st.get("eval", 0) and _score_value(st["learned"]) < _score_value(st["rule"]):
        return REASON_RULE_BETTER
    return None


def chosen_method(default_method: str, station_methods: Dict[str, str], key: str) -> str:
    """設定で選んだ方式(局の指定 → 全体の既定)。"""
    method = station_methods.get(key, METHOD_DEFAULT)
    if method not in (METHOD_RULE, METHOD_LEARNED):
        method = default_method if default_method in (METHOD_RULE, METHOD_LEARNED) else METHOD_LEARNED
    return method


_cache: Dict[int, LearnedModel] = {}


def load_active_model(db) -> Optional[Tuple[LearnedModel, dict]]:
    """使う印のあるモデルと details。無い・読めない・特徴の版が違う・前のライブラリのモデル・Rust の部品が無い時は None。"""
    if db is None:
        return None
    try:
        row = db.get_latest_model(active_only=True)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the learned model", exc_info=True)
        return None
    if (row is None or row.get("feature_version") != MODEL_FEATURE_VERSION or row.get("library") != LIBRARY
            or not learning_available()):
        return None
    model = _cache.get(row["id"])
    if model is None:
        full = db.get_latest_model(active_only=True, with_data=True)
        try:
            model = LearnedModel.from_blob(full["model_data"])
        except Exception:  # noqa: BLE001
            logger.warning("Could not load the learned model id=%s", row["id"], exc_info=True)
            return None
        _cache.clear()
        _cache[row["id"]] = model
    try:
        details = json.loads(row["details"] or "{}")
    except ValueError:
        details = {}
    return model, details


def retrain(db, stop_event=None, progress: Optional[Callable[[str, int, int], None]] = None) -> TrainingResult:
    """記録から学習し直し、結果を記録 DB に保存する(設定の「学習し直す」)。ルールより良い時だけ使う印を付ける。"""
    library_version = str(native_module().VERSION)

    def load_progress(k, total):
        if progress is not None:
            progress("load", k, total)

    items = load_items(db, stop_event, load_progress)
    result = train_and_evaluate(items, stop_event, (lambda s: progress(s, 0, 0)) if progress else None)
    problem = result.adoption_problem()
    db.save_model(LIBRARY, library_version, MODEL_FEATURE_VERSION, len(items), result.samples,
                  result.eval_recordings, result.learned.recall if result.eval_recordings else None,
                  result.rule.recall if result.eval_recordings else None, json.dumps(result.details()),
                  problem is None, result.model.to_blob() if result.model else None)
    _cache.clear()
    if problem is None:
        logger.info("Learned model adopted: %d recordings (%d left out to check), recall %.1f%% vs rules %.1f%%, "
                    "programme cut %.1f s vs %.1f s, %.1f s", len(items), result.eval_recordings,
                    result.learned.recall * 100, result.rule.recall * 100, result.learned.wrong_cut,
                    result.rule.wrong_cut, result.seconds)
    else:
        logger.info("Learned model not used: %s", problem)
    return result


def file_proposals(db, features: RecordingFeatures, window: Optional[ProgramWindow], profile: StationProfile,
                   thresholds: Thresholds, original_network_id: Optional[int], service_id: Optional[int],
                   broadcast_type: Optional[str], genre: Optional[int], default_method: str,
                   station_methods: Dict[str, str]) -> Optional[List[KeepProposal]]:
    """録画を開いた時、学習で案を作る局ならその案。ルールで作る時は None。"""
    key = station_key(original_network_id, service_id)
    if chosen_method(default_method, station_methods, key) != METHOD_LEARNED:
        return None
    loaded = load_active_model(db)
    if loaded is None:
        return None
    model, details = loaded
    if station_problem(details, key) is not None:
        return None
    return learned_proposals(model, features, window, profile, thresholds, key, broadcast_type, genre)
