"""自動カットの案: 承認・棄却・すべて承認・Undo、記録、録画を開いた時の案 (計画 A6)。"""

from pathlib import Path

import pytest

from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.proposals import build_file_proposals
from ts_smartcut.autocut.recorder import AutoCutRecorder
from ts_smartcut.core.cut_manager import CutManager
from ts_smartcut.errors import ValidationError
from ts_smartcut.models import (
    PROPOSAL_ACCEPTED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    CutProposal,
    CutRange,
    FileCutInfo,
)


def _info(path: Path = Path("x.ts"), frames: int = 10000) -> FileCutInfo:
    return FileCutInfo(file_path=path, total_frames=frames, fps=29.97, resolution=(1440, 1080),
                       video_codec="mpeg2video", audio_codec="aac", bitrate=1, duration=frames / 29.97,
                       file_size=1)


def _proposals():
    return [CutProposal(100, 3000, 0.9, "本編 A"), CutProposal(4000, 8000, 0.5, "本編 B"),
            CutProposal(8500, 9000, 0.3, "本編 C")]


def test_accept_reject_and_undo_redo():
    m = CutManager(_info())
    m.set_proposals(_proposals())
    a, b, c = m.proposals
    m.accept_proposal(a)
    assert [(x.start_frame, x.end_frame) for x in m.cuts] == [(100, 3000)]
    assert a.status == PROPOSAL_ACCEPTED and m.pending_proposals == [b, c]
    m.reject_proposal(b)
    assert b.status == PROPOSAL_REJECTED and m.pending_proposals == [c]

    m.undo()
    assert b.status == PROPOSAL_PENDING
    m.undo()
    assert m.cuts == [] and a.status == PROPOSAL_PENDING
    m.redo()
    assert [(x.start_frame, x.end_frame) for x in m.cuts] == [(100, 3000)] and a.status == PROPOSAL_ACCEPTED
    types = [e["event_type"] for e in m.edit_events]
    assert types[:3] == ["proposals_shown", "proposal_accepted", "proposal_rejected"]


def test_accept_all_is_one_undo_and_skips_overlaps():
    m = CutManager(_info())
    m.add_cut(4500, 5000)  # 案 B と重なる手で付けた区間
    m.set_proposals(_proposals())
    assert len(m.proposals) == 2  # 重なる案は出さない
    m.proposals.append(CutProposal(4900, 5500, 0.4, "重なる案"))
    skipped = m.accept_all_proposals()
    assert [(p.start_frame, p.end_frame) for p in skipped] == [(4900, 5500)]
    assert [(x.start_frame, x.end_frame) for x in m.cuts] == [(100, 3000), (4500, 5000), (8500, 9000)]
    m.undo()
    assert [(x.start_frame, x.end_frame) for x in m.cuts] == [(4500, 5000)]
    assert all(p.status == PROPOSAL_PENDING for p in m.proposals)


def test_accepting_an_overlapping_or_decided_proposal_is_refused():
    m = CutManager(_info())
    m.set_proposals(_proposals())
    a = m.proposals[0]
    m.add_cut(2000, 3500)
    with pytest.raises(ValidationError):
        m.accept_proposal(a)
    assert a.status == PROPOSAL_PENDING
    b = m.proposals[1]
    m.reject_proposal(b)
    with pytest.raises(ValidationError):
        m.accept_proposal(b)


@pytest.mark.parametrize("cuts, expected_type", [
    ([(100, 3000), (4000, 8000)], "proposal"),
    ([(100, 2990), (4000, 8000)], "proposal_modified"),
    ([(100, 2990)], "proposal_modified"),
])
def test_recording_keeps_the_accepted_proposals_and_the_corrections(tmp_path, cuts, expected_type):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"ts")
    info = _info(source)
    info.cuts = [CutRange(a, b) for a, b in cuts]
    proposals = [p.to_dict() for p in _proposals()]
    proposals[0]["status"] = proposals[1]["status"] = PROPOSAL_ACCEPTED
    proposals[2]["status"] = PROPOSAL_REJECTED
    db = CutHistoryDB(tmp_path / "cut_history.db")
    session = AutoCutRecorder(db).record_batch_done(info, "smart_cut", proposals=proposals)
    assert db.get_session(session)["source_type"] == expected_type
    rows = db.get_session_cut_ranges(session)
    assert rows[0]["proposal_start_frame"] == 100 and rows[0]["proposal_end_frame"] == 3000
    assert rows[0]["diff_frames"] == abs(cuts[0][1] - 3000)


def test_recording_without_proposals_is_manual(tmp_path):
    source = tmp_path / "rec.ts"
    source.write_bytes(b"ts")
    info = _info(source)
    info.cuts = [CutRange(100, 200)]
    db = CutHistoryDB(tmp_path / "cut_history.db")
    session = AutoCutRecorder(db).record_batch_done(info, "smart_cut")
    assert db.get_session(session)["source_type"] == "manual"
    assert db.get_session_cut_ranges(session)[0]["diff_frames"] is None


def test_registered_video_uses_the_stored_features(tmp_path):
    """登録済みの動画は、録画を読まずに記録 DB の特徴から案を作る。"""
    from datetime import datetime, timedelta

    from tests.test_autocut_rules import PARTS, _features, _keep

    f, spans = _features(PARTS)
    db = CutHistoryDB(tmp_path / "cut_history.db")
    with db.get_connection() as conn:
        rid = db.insert_recording(conn, {"file_name": "a.ts", "file_size": 1, "file_hash": "h", "created_at": "x",
                                         "service_id": 211, "original_network_id": 4})
    db.save_features(rid, f.to_blob(), 3, True, f.frame_count)

    class Event:
        def __init__(self, start):
            self.start, self.duration = start, timedelta(seconds=1082)

    class Info:
        recording_start = datetime(2026, 10, 1, 1, 0, 0)
        event = Event(recording_start + timedelta(seconds=4))
        service_id, original_network_id = 211, 4

    proposals = build_file_proposals(db, tmp_path / "missing.ts", f.fps, Info(), {"id": rid})
    keep = _keep(spans)
    assert [(p.start_frame, p.end_frame) for p in proposals] == [(a, b - 1) for a, b in keep]
    assert all(p.status == PROPOSAL_PENDING and p.reason for p in proposals)
