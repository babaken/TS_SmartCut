"""境目の候補・ルールによる判定・一致率の評価 (自動カット計画 A5・A7)。

CM の位置が分かっている合成の特徴(番組の前後、ロゴの出る本編、無音と場面の切り替わりで
区切った 15・30 秒の CM、本編と CM の間の提供クレジット)で、案がフレーム単位で合うこと、
評価の数え方が正しいことを確かめる。
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import dataclasses

from ts_smartcut.autocut.candidates import Boundary, find_boundaries, segments_from_boundaries
from ts_smartcut.autocut.evaluate import EXACT, MISS, NEAR, compare, evaluate_all
from ts_smartcut.autocut.features import RecordingFeatures
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.logo import LogoTemplate
from ts_smartcut.autocut.proposals import program_window, station_profile
from ts_smartcut.autocut.rules import CM, MARGIN, PROGRAM, ProgramWindow, StationProfile, propose
from ts_smartcut.autocut.rules import classify_segments

FPS = 30000 / 1001


def _frames(seconds: float) -> int:
    return int(round(seconds * FPS))


def _features(parts, logo=True, seed=0):
    """parts: [(種類, 秒)]。種類は "margin"・"program"・"cm"・"credit"(本編と CM の間の提供)・
    "silent"(音の無い番宣)。区間の始まりに 1 秒の無音(前の区間の最後)と場面の切り替わり。
    戻り値: (特徴, 区間ごとの (種類, 開始フレーム, 終了フレーム))"""
    rng = np.random.default_rng(seed)
    spans = []
    t = 0.0
    for kind, seconds in parts:
        spans.append((kind, _frames(t), _frames(t + seconds)))
        t += seconds
    n = spans[-1][2]
    scene = rng.uniform(0, 8, n).astype(np.float16)
    logo_score = np.zeros((4, n), np.uint8)
    audio = np.full(int(t * 100) + 1, 2 * (100 - 20), np.uint8)  # −20dB
    for kind, a, b in spans:
        if a > 0:
            scene[a] = 120.0
            s = int(a / FPS * 100)
            audio[max(s - 100, 0):s] = 0  # 境目の直前 1 秒の無音(−100dB)
        if kind == "program" and logo:
            logo_score[1, a:b] = 200
        if kind == "silent":
            audio[int(a / FPS * 100):int(b / FPS * 100)] = 0
    template = None
    if logo:
        mask = np.zeros((4, 10, 10), bool)
        mask[1, 2:8, 2:8] = True
        template = LogoTemplate(np.zeros((4, 10, 10), np.float32), np.zeros((4, 10, 10), np.float32),
                                mask, mask.copy(), np.array([0, 0.8, 0, 0], np.float32), 1000)
    f = RecordingFeatures(
        fps=FPS, frame_count=n, complete=True, audio_rate=100, audio_offset=0.0, audio_level=audio,
        scene_change=scene, decoded_size=(80, 60), corner_size=(20, 15),
        corner_kept_mean=np.zeros((4, 15, 20), np.float16), corner_kept_std=np.zeros((4, 15, 20), np.float16),
        corner_kept_count=0, corner_other_mean=np.zeros((4, 15, 20), np.float16),
        corner_other_std=np.zeros((4, 15, 20), np.float16), corner_other_count=0,
        logo_score=logo_score, logo_template=template,
    )
    return f, spans


PARTS = [
    ("margin", 5), ("program", 200), ("cm", 15), ("cm", 30), ("cm", 15),
    ("program", 400), ("cm", 30), ("cm", 30), ("program", 300), ("cm", 60), ("margin", 20),
]
WINDOW = ProgramWindow(4.0, 1086.0)  # 番組の前後: 最初の 5 秒と最後の 20 秒


def _keep(spans, kinds=("program", "credit")):
    """正解の残す区間(続く本編・提供はつなぐ)。"""
    result = []
    for kind, a, b in spans:
        if kind in kinds:
            if result and result[-1][1] == a:
                result[-1] = (result[-1][0], b)
            else:
                result.append((a, b))
    return result


def test_silences_and_boundaries_are_found_at_the_scene_changes():
    f, spans = _features(PARTS)
    boundaries = find_boundaries(f)
    assert [b.frame for b in boundaries] == [a for _k, a, _b in spans[1:]]
    assert all(abs(b.silence - 1.0) < 0.02 for b in boundaries)  # 1 秒の無音
    assert all(b.scene > 100 for b in boundaries)  # 場面の切り替わりのフレーム


def test_rules_keep_the_program_and_cut_cm_and_margins_frame_exactly():
    f, spans = _features(PARTS)
    proposals = propose(f, WINDOW, StationProfile(end_offset=0))
    assert [(p.start, p.end) for p in proposals] == _keep(spans)
    assert all(p.confidence >= 0.7 for p in proposals)
    assert "ロゴあり" in proposals[0].reason and "CM" not in proposals[0].reason.split("前:")[0]


def test_end_offset_ends_the_keep_range_before_the_cm():
    f, spans = _features(PARTS)
    proposals = propose(f, WINDOW, StationProfile(end_offset=2))
    keep = _keep(spans)
    assert [(p.start, p.end) for p in proposals] == [(a, b - 2) for a, b in keep]


def test_station_without_logo_cuts_only_cm_runs_and_keeps_an_op_of_90_seconds():
    """ロゴの出ない局(CS-A 等): 15 秒の倍数が続く所と、番組の前後から続く所だけを切る。
    本編の中の OP(90 秒 = 15 秒×6)は切らない。"""
    parts = [("margin", 5), ("program", 100), ("program", 90), ("program", 600),
             ("cm", 15), ("cm", 30), ("cm", 15), ("program", 500), ("cm", 30), ("cm", 15), ("margin", 20)]
    f, spans = _features(parts, logo=False)
    # 番組の始まりの 100 秒(5 秒の倍数)は、番組の前後の隣でも切らない
    proposals = propose(f, ProgramWindow(4.0, 1401.0), StationProfile(end_offset=0))
    assert [(p.start, p.end) for p in proposals] == _keep(spans)


def test_silent_cards_after_the_program_are_cut():
    parts = [("margin", 5), ("program", 700), ("silent", 13), ("silent", 3), ("cm", 30), ("cm", 15),
             ("margin", 20)]
    f, spans = _features(parts, logo=False)
    proposals = propose(f, ProgramWindow(4.0, 769.0), StationProfile(end_offset=0))
    assert [(p.start, p.end) for p in proposals] == _keep(spans)


def test_short_segment_between_program_and_cm_follows_the_station():
    """本編と CM の間の 10 秒(提供クレジット)は、記録が無い・残す局なら残し、切る局なら切る。"""
    parts = [("margin", 5), ("program", 300), ("credit", 10), ("cm", 30), ("cm", 30), ("program", 300),
             ("margin", 20)]
    f, spans = _features(parts)
    window = ProgramWindow(4.0, 684.0)
    kept = propose(f, window, StationProfile(end_offset=0))
    assert [(p.start, p.end) for p in kept] == _keep(spans)
    assert "提供" in kept[0].reason or kept[0].confidence <= 0.3
    cut = propose(f, window, StationProfile(end_offset=0, short_between_kept=0.0))
    assert [(p.start, p.end) for p in cut] == _keep(spans, kinds=("program",))


def test_tiny_segments_join_the_segment_before():
    """CM の後ろの黒い画面(2 秒未満)は手前の CM につなげる。最初と最後の区間はそのまま。"""
    f, _spans = _features(PARTS)
    f = dataclasses.replace(f, fps=10.0, frame_count=330)
    boundaries = [Boundary(frame, 1.0, 0.0) for frame in (5, 155, 170, 320)]
    merged = segments_from_boundaries(f, boundaries)
    assert [(s.start, s.end) for s in merged] == [(0, 5), (5, 170), (170, 320), (320, 330)]


def test_classification_kinds():
    f, spans = _features(PARTS)
    segments = segments_from_boundaries(f, find_boundaries(f))
    kinds = [d.kind for d in classify_segments(f, segments, WINDOW)]
    expected = {"margin": MARGIN, "program": PROGRAM, "cm": CM}
    assert kinds == [expected[k] for k, _a, _b in spans]


# ---- 評価 ---------------------------------------------------------------------

class _P:
    def __init__(self, start, end):
        self.start, self.end = start, end


def test_compare_counts_cut_time_and_edges():
    n, fps = 1000, 10.0
    truth = [(100, 400, False), (500, 900, False)]
    proposals = [_P(100, 402), _P(530, 900)]  # 境目: ±0, ±2, ±30(3 秒、外れ), ±0
    s = compare(truth, proposals, n, fps)
    assert s.truth_cut == pytest.approx(30.0)  # 0-100, 400-500, 900-1000
    assert s.proposed_cut == pytest.approx((100 + 128 + 100) / fps)
    assert s.correct_cut == pytest.approx((100 + 98 + 100) / fps)
    assert s.wrong_cut == pytest.approx(30 / fps)  # 500-530
    assert s.edges == {EXACT: 2, NEAR: 1, "±1秒": 0, MISS: 1}
    assert s.extra_edges == 1  # 530
    assert s.recall == pytest.approx(298 / 300) and s.precision == pytest.approx(298 / 328)


def test_compare_ignores_excluded_ranges():
    truth = [(100, 400, False), (400, 600, True)]
    s = compare(truth, [_P(100, 400)], 1000, 10.0)
    assert s.truth_cut == pytest.approx(50.0)  # 0-100, 600-1000(基準外の 400-600 は数えない)
    assert s.wrong_cut == 0 and s.recall == 1.0


def test_compare_ignores_ranges_that_end_before_the_start():
    # 負の終わりを numpy が末尾から数え直し、録画のほぼ全体を「残す」にしていた
    truth = [(100, 400, False), (-50, -10, False), (-30, -5, True)]
    s = compare(truth, [_P(100, 400), _P(-40, -20)], 1000, 10.0)
    plain = compare([(100, 400, False)], [_P(100, 400)], 1000, 10.0)
    assert (s.truth_cut, s.proposed_cut, s.correct_cut, s.wrong_cut) == pytest.approx(
        (plain.truth_cut, plain.proposed_cut, plain.correct_cut, plain.wrong_cut))
    assert s.truth_cut == pytest.approx(70.0)


def _record(db, features, keep, service_id=211, start="2026-10-01T00:59:56+09:00", complete=True):
    rs = datetime.fromisoformat(start)
    with db.get_connection() as conn:
        rid = db.insert_recording(conn, {
            "file_name": f"{start}.ts", "file_size": 1, "file_hash": start, "service_id": service_id,
            "original_network_id": 4, "service_name": "局", "broadcast_type": "bs", "program_name": "番組",
            "recording_start": rs.isoformat(), "broadcast_start": (rs + timedelta(seconds=4)).isoformat(),
            "broadcast_duration": 1100, "created_at": "x", "updated_at": "x",
        })
        sid = db.insert_session(conn, {"recording_id": rid, "finalized_at": "x", "processing_mode": "smart_cut",
                                       "source_type": "manual", "is_active": 1})
        db.insert_cut_ranges(conn, sid, [{"start_frame": a, "end_frame": b} for a, b in keep])
    db.save_features(rid, features.to_blob(), 3, complete, features.frame_count)
    return rid


def test_evaluate_all_and_station_profile_from_the_history(tmp_path):
    db = CutHistoryDB(tmp_path / "cut_history.db")
    f, spans = _features(PARTS)
    keep = _keep(spans)
    # 利用者は CM の直前の 2 フレームを残さない
    first = _record(db, f, [(a, b - 2) for a, b in keep], start="2026-10-01T00:59:56+09:00")
    second = _record(db, f, [(a, b - 2) for a, b in keep], start="2026-10-08T00:59:56+09:00")
    assert station_profile(db, 4, 211).end_offset == 2
    assert station_profile(db, 4, 211, exclude_recording=first).end_offset == 2
    assert station_profile(db, 4, 999).end_offset == 1  # 記録の無い局は既定値

    results = evaluate_all(db)
    assert [r.recording_id for r in results] == [first, second]
    for r in results:
        assert r.score.recall == pytest.approx(1.0) and r.score.wrong_cut == 0
        assert r.score.edges[MISS] == 0


def test_partial_features_are_left_out_of_the_history(tmp_path):
    """途中までの特徴(計算が止まった受信エラーの録画など)は、評価・しきい値の補正・局の傾向・
    局のロゴの型・局の一覧に使わない(利用者の決定 2026-10-15)。"""
    from ts_smartcut.autocut.tuning import load_samples

    db = CutHistoryDB(tmp_path / "cut_history.db")
    f, spans = _features(PARTS)
    keep = _keep(spans)
    complete = [_record(db, f, [(a, b - 2) for a, b in keep], start=f"2026-10-0{d}T00:59:56+09:00")
                for d in (1, 8)]
    _record(db, f, [(a, b - 10) for a, b in keep], start="2026-10-09T00:59:56+09:00", complete=False)

    assert [r.recording_id for r in evaluate_all(db)] == complete
    assert station_profile(db, 4, 211).end_offset == 2
    assert [s.recording_id for s in load_samples(db, "")] == complete
    assert [r["recording_id"] for r in db.get_station_feature_blobs(4, 211)] == complete
    assert [r["recordings"] for r in db.list_stations()] == [2]


def test_program_window_from_iso_times():
    w = program_window("2026-10-01T00:59:55.4+09:00", "2026-10-01T01:00:00+09:00", 2880)
    assert w.start == pytest.approx(4.6) and w.end == pytest.approx(2884.6)
    assert program_window(None, "2026-10-01T01:00:00+09:00", 2880) is None


@pytest.mark.parametrize("seconds,text", [
    (119.6, "2分00秒"), (899.7, "15分00秒"), (59.6, "1分00秒"), (59.4, "59秒"), (61.2, "1分01秒"), (9.94, "9.9秒"),
])
def test_durations_in_reasons_never_show_60_seconds(seconds, text):
    # 分と秒を別々に丸め、案の説明が「1分60秒」「14分60秒」になっていた(2026-10-10)
    from ts_smartcut.autocut.rules import _format_seconds

    assert _format_seconds(seconds) == text
