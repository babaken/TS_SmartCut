"""提案(案)を作るための材料 (自動カット計画 A5・A6)。

- program_window: EIT の放送時間を、映像の先頭からの秒にする(番組の前後の判定)。
- station_profile: 局ごとの値を、その局の過去のカット(記録 DB)から測る。
- しきい値(thresholds.py)は局ごとに記録 DB から読む(局の値 → 全局共通 → 初期値)。
- recording_proposals: 記録 DB の録画の特徴から案を作る(評価 A7 と、登録済みの動画を開いた時)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from ts_smartcut.autocut.candidates import find_boundaries, recording, threshold_values
from ts_smartcut.autocut.features import RecordingFeatures
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.rules import (
    KeepProposal,
    ProgramWindow,
    StationProfile,
    propose,
)
from ts_smartcut.autocut.thresholds import DEFAULT_THRESHOLDS, Thresholds, load_thresholds

logger = logging.getLogger(__name__)


def _parse(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def program_window(recording_start, broadcast_start, duration_seconds) -> Optional[ProgramWindow]:
    """EIT の放送時間 (映像の先頭からの秒)。分からなければ None。録画の先頭(TOT から求めた
    録画開始)と映像の先頭の差(1 秒未満)は無視する。"""
    rs, bs = _parse(recording_start), _parse(broadcast_start)
    if rs is None or bs is None or not duration_seconds:
        return None
    start = (bs - rs).total_seconds()
    return ProgramWindow(start, start + float(duration_seconds))


def window_from_program_info(info) -> Optional[ProgramWindow]:
    """ProgramInfo(録画を開いた時に読む番組情報)から。"""
    event = getattr(info, "event", None)
    if info is None or event is None or event.start is None or event.duration is None:
        return None
    return program_window(info.recording_start, event.start, event.duration.total_seconds())


def keep_ranges(db, recording_id: int) -> List[Tuple[int, int, bool]]:
    """記録した残す区間 (開始, 終了, 基準外)。話ごとに分けて変換した録画は、すべての話の区間。"""
    return [(r["start_frame"], r["end_frame"], bool(r["is_excluded"]))
            for r in db.get_active_cut_ranges(recording_id)]


def measure(features: RecordingFeatures, keep: Sequence[Tuple[int, int, bool]], window: Optional[ProgramWindow],
            thresholds: Thresholds = DEFAULT_THRESHOLDS, boundaries=None) -> Tuple[List[int], List[bool]]:
    """局の値の材料 (native/src/rules.rs の measure_end_offsets・measure_short_between):
    - 残す区間の終わりが、次の境目(新しい場面)の何フレーム手前だったか(3 フレーム以内の境目)
    - ルールで「本編と CM の間の短い区間」になった区間を、利用者が残したか(区間ごと)
    boundaries は同じ thresholds で求めた境目の候補(省くと求める)。"""
    if boundaries is None:
        boundaries = find_boundaries(features, thresholds)
    return native_module().rule_measures(
        recording(features), [(int(a), int(b), bool(ex)) for a, b, ex in keep],
        None if window is None else (float(window.start), float(window.end)),
        [(b.frame, b.silence, b.scene) for b in boundaries], threshold_values(thresholds))


Measures = Dict[int, Tuple[List[int], List[bool]]]  # 録画 → (区間の終わりの差, 短い区間を残したか)


def item_measures(items, thresholds: Thresholds = DEFAULT_THRESHOLDS, boundaries_for=None) -> Measures:
    """局の録画 (id, 特徴, 残した区間, 放送時間) ごとに、局の値の材料を測る。boundaries_for(id) は
    同じ thresholds で求めた境目の候補(覚えておいたもの。省くと求める)。"""
    result: Measures = {}
    for recording_id, features, keep, window in items:
        boundaries = boundaries_for(recording_id) if boundaries_for is not None else None
        result[recording_id] = measure(features, keep, window, thresholds, boundaries)
    return result


def profile_from_measures(measures: Measures, exclude_recording: Optional[int] = None) -> StationProfile:
    """測った材料から局の値(区間の終わりの差の中央値、短い区間を残した割合。native/src/rules.rs)。
    exclude_recording の録画の材料は使わない。"""
    end_offset, kept = native_module().rule_profile(
        [(int(rid), list(o), list(b)) for rid, (o, b) in measures.items()], exclude_recording)
    return StationProfile(end_offset=end_offset, short_between_kept=kept)


def station_items(db, original_network_id: Optional[int], service_id: Optional[int]):
    """局の録画 (id, 特徴, 残した区間, 放送時間)。特徴を読めない・途中までの録画は除く。"""
    if service_id is None:
        return []
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.recording_start, r.broadcast_start, r.broadcast_duration, f.data
            FROM recordings r JOIN features f ON f.recording_id = r.id
            WHERE r.service_id = ? AND r.original_network_id IS ? AND f.feature_version >= 2 AND f.is_complete = 1;
            """,
            (service_id, original_network_id),
        ).fetchall()
    items = []
    for row in rows:
        try:
            features = RecordingFeatures.from_blob(row["data"])
        except Exception:  # noqa: BLE001
            logger.warning("Could not read features: recording_id=%s", row["id"], exc_info=True)
            continue
        window = program_window(row["recording_start"], row["broadcast_start"], row["broadcast_duration"])
        items.append((row["id"], features, keep_ranges(db, row["id"]), window))
    return items


def station_profile(db, original_network_id: Optional[int], service_id: Optional[int],
                    exclude_recording: Optional[int] = None,
                    thresholds: Optional[Thresholds] = None) -> StationProfile:
    """局の過去のカットから測った値。exclude_recording は測るのに使わない録画(評価で
    その録画自身を除く)。thresholds を省くと記録 DB の局の値。記録が無ければ既定値。"""
    if service_id is None:
        return StationProfile()
    if thresholds is None:
        thresholds = load_thresholds(db, original_network_id, service_id)
    measures = item_measures(station_items(db, original_network_id, service_id), thresholds)
    return profile_from_measures(measures, exclude_recording)


def recording_proposals(db, recording: dict, features: RecordingFeatures,
                        exclude_self: bool = True,
                        thresholds: Optional[Thresholds] = None) -> List[KeepProposal]:
    """記録 DB の録画の案。exclude_self なら、局の値をその録画以外のカットから測る(評価)。
    thresholds を省くと記録 DB の局の値。"""
    onid, sid = recording.get("original_network_id"), recording.get("service_id")
    if thresholds is None:
        thresholds = load_thresholds(db, onid, sid)
    window = program_window(recording.get("recording_start"), recording.get("broadcast_start"),
                            recording.get("broadcast_duration"))
    profile = station_profile(db, onid, sid, exclude_recording=recording["id"] if exclude_self else None,
                              thresholds=thresholds)
    return propose(features, window, profile, thresholds=thresholds)


def build_file_proposals(db, path, fps: float, program_info, registration: Optional[dict] = None,
                         stop_event=None, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", progress=None,
                         default_method: str = "rule", station_methods: Optional[Dict[str, str]] = None):
    """録画を開いた時の案(A6)。CutProposal の一覧を返す。止めた時・作れない時は None。

    登録済みの動画(registration)で特徴が記録 DB にあればそれを使い、無ければ録画を読んで
    特徴を計算する(局の型、無ければ取り込んだ局ロゴの型でロゴの一致度を出す。1本 20〜90 秒)。
    default_method・station_methods は設定の方式(model.METHOD_*、A8)。学習を選んだ局で使えるモデルが
    あれば学習で、無ければルールで案を作る。"""
    from ts_smartcut.autocut.features import compute_features
    from ts_smartcut.autocut.lgd_import import load_imported_logo
    from ts_smartcut.autocut.logo import load_station_logo
    from ts_smartcut.models import CutProposal

    onid = getattr(program_info, "original_network_id", None)
    sid = getattr(program_info, "service_id", None)
    features = None
    if registration is not None:
        row = db.get_features_row(registration["id"])
        if row is not None and row.get("feature_version", 0) >= 2:
            features = RecordingFeatures.from_blob(row["data"])
    if features is None:
        station = load_station_logo(db, onid, sid) if sid is not None else None
        if station is None and sid is not None:
            # 局の型がまだ無い局(録画が 2 本に満たない)は、取り込んだ局ロゴ(.lgd)の型で
            station = load_imported_logo(db, sid)
        features = compute_features(path, fps, [], ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path,
                                    stop_event=stop_event, service_id=sid, station_template=station,
                                    progress=progress)
        if stop_event is not None and stop_event.is_set():
            return None
    window = window_from_program_info(program_info)
    thresholds = load_thresholds(db, onid, sid)
    profile = station_profile(db, onid, sid, thresholds=thresholds)
    proposals = None
    if default_method != "rule" or station_methods:
        from ts_smartcut.autocut.model import file_proposals

        event = getattr(program_info, "event", None)
        genres = getattr(event, "genres", None) or []
        proposals = file_proposals(db, features, window, profile, thresholds, onid, sid,
                                   getattr(program_info, "broadcast_type", None), genres[0] if genres else None,
                                   default_method, station_methods or {})
    if proposals is None:
        proposals = propose(features, window, profile, thresholds=thresholds)
    return [CutProposal(p.start, p.end, round(p.confidence, 2), p.reason) for p in proposals]
