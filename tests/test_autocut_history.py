"""A2: local history database and finalized batch recording."""

import sqlite3
from pathlib import Path

import pytest

from ts_smartcut.autocut.history_db import CURRENT_SCHEMA_VERSION, CutHistoryDB
from ts_smartcut.autocut.recorder import AutoCutRecorder
from ts_smartcut.errors import ProjectError
from ts_smartcut.models import CutRange, FileCutInfo


def _file_info(path: Path) -> FileCutInfo:
    return FileCutInfo(
        file_path=path,
        total_frames=3000,
        fps=30.0,
        resolution=(1920, 1080),
        video_codec="h264",
        audio_codec="aac",
        bitrate=4_000_000,
        duration=100.0,
        file_size=path.stat().st_size,
        cuts=[CutRange(100, 2900, "program", is_excluded=True)],
    )


def test_database_creates_schema_and_all_history_tables(tmp_path):
    db = CutHistoryDB(tmp_path / "cut_history.db")

    with db.get_connection() as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert version == CURRENT_SCHEMA_VERSION
    assert {"recordings", "cut_sessions", "cut_ranges", "edit_events", "features", "logos", "models"} <= tables


def test_recording_redo_replaces_previous_session_and_saves_events(tmp_path):
    source = tmp_path / "recording.ts"
    source.write_bytes(b"transport stream fixture")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    recorder = AutoCutRecorder(db)
    info = _file_info(source)
    events = [{"event_order": 0, "event_type": "add_cut", "details": {"start_frame": 100}}]

    first_session = recorder.record_batch_done(info, "smart_cut", edit_events=events)
    second_session = recorder.record_batch_done(info, "simple_copy", edit_events=events)

    assert first_session is not None and second_session is not None
    with db.get_connection() as conn:
        recordings = conn.execute("SELECT id, created_at, updated_at FROM recordings ORDER BY id").fetchall()
        sessions = conn.execute("SELECT id, recording_id, is_active FROM cut_sessions ORDER BY id").fetchall()
        ranges = conn.execute("SELECT start_frame, end_frame, is_excluded FROM cut_ranges WHERE session_id=?", (second_session,)).fetchall()
        saved_events = conn.execute("SELECT event_type, details FROM edit_events WHERE session_id=?", (second_session,)).fetchall()

    # 同じ動画は1行。最初に登録した日時と、最後にカットを確定した日時を持つ
    assert len(recordings) == 1
    assert recordings[0]["created_at"] <= recordings[0]["updated_at"]
    assert [s["recording_id"] for s in sessions] == [recordings[0]["id"]] * 2
    assert sessions[0]["is_active"] == 0 and sessions[1]["is_active"] == 1
    assert ranges[0]["start_frame"] == 100 and ranges[0]["end_frame"] == 2900 and ranges[0]["is_excluded"] == 1
    assert saved_events[0]["event_type"] == "add_cut"
    assert '"start_frame": 100' in saved_events[0]["details"]


def test_database_is_sqlite_file_in_requested_directory(tmp_path):
    db = CutHistoryDB(tmp_path / "nested" / "cut_history.db")
    assert db.db_path.is_file()
    assert db.db_path.parent == tmp_path / "nested"


def test_version_zero_database_migrates_to_current_schema(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_info (version INTEGER PRIMARY KEY, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_info VALUES (0, 'old')")

    db = CutHistoryDB(path)

    with db.get_connection() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recordings'").fetchone()


def test_database_newer_than_app_is_rejected(tmp_path):
    path = tmp_path / "new.db"
    CutHistoryDB(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_info VALUES (?, 'future')", (CURRENT_SCHEMA_VERSION + 1,))

    with pytest.raises(ProjectError) as e:
        CutHistoryDB(path)
    assert e.value.code == "E403"


def test_v2_database_merges_redone_recordings_into_one_row(tmp_path):
    """v2 までの「カットし直すと行を足し、古い行に is_replaced=1」の DB を v3 に移す。"""
    path = tmp_path / "v2.db"
    db = CutHistoryDB(path)
    with db.get_connection() as conn:
        conn.execute("DELETE FROM schema_info WHERE version > 2")
        for rec_id, replaced, created in ((1, 1, "2026-10-01T00:00:00+00:00"), (2, 0, "2026-10-02T00:00:00+00:00"),
                                          (3, 0, "2026-10-03T00:00:00+00:00")):
            conn.execute(
                "INSERT INTO recordings (id, file_name, file_size, file_hash, created_at, is_replaced) "
                "VALUES (?, 'a.ts', ?, 'h', ?, ?)",
                (rec_id, 10 if rec_id < 3 else 20, created, replaced),
            )
        conn.execute("UPDATE recordings SET updated_at = NULL")
        for sess_id, rec_id, active, finalized in ((1, 1, 0, "2026-10-01T01:00:00+00:00"),
                                                   (2, 2, 1, "2026-10-02T01:00:00+00:00")):
            conn.execute(
                "INSERT INTO cut_sessions (id, recording_id, finalized_at, processing_mode, source_type, is_active) "
                "VALUES (?, ?, ?, 'smart_cut', 'manual', ?)",
                (sess_id, rec_id, finalized, active),
            )
        conn.execute("INSERT INTO features (recording_id, computed_at, data) VALUES (1, 'x', x'00')")

    db = CutHistoryDB(path)

    with db.get_connection() as conn:
        recordings = conn.execute("SELECT id, created_at, updated_at FROM recordings ORDER BY id").fetchall()
        sessions = conn.execute("SELECT id, recording_id FROM cut_sessions ORDER BY id").fetchall()
        features = conn.execute("SELECT recording_id FROM features").fetchall()
    assert [r["id"] for r in recordings] == [2, 3]
    assert recordings[0]["created_at"] == "2026-10-01T00:00:00+00:00"  # 最初に登録した日時を引き継ぐ
    assert recordings[0]["updated_at"] == "2026-10-02T01:00:00+00:00"
    assert recordings[1]["updated_at"] == "2026-10-03T00:00:00+00:00"  # カットの記録が無ければ登録日時
    assert [s["recording_id"] for s in sessions] == [2, 2]
    assert features == []


def test_v4_database_keeps_logos_as_current_and_recreates_models(tmp_path, monkeypatch):
    """v4 → v5(2026-10-09 の決定): 局のロゴの型は今の型(is_current=1)として残り、models は
    1つのモデルの形に作り直す(行があれば models_v4 に残す)。"""
    from ts_smartcut.autocut import history_db

    path = tmp_path / "v4.db"
    with monkeypatch.context() as mp:
        mp.setattr(history_db, "CURRENT_SCHEMA_VERSION", 4)
        mp.setattr(CutHistoryDB, "_migrate_to_v5", lambda self, conn: None)
        db = CutHistoryDB(path)
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO logos (service_id, original_network_id, created_at, updated_at, valid_from, valid_to, "
                "recordings_count, logo_data) VALUES (211, 4, 'c', 'u', 'f', 't', 2, x'00')"
            )
            conn.execute("INSERT INTO models (target_type, trained_at) VALUES ('station', 'x')")

    db = CutHistoryDB(path)

    with db.get_connection() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0] == CURRENT_SCHEMA_VERSION
        logo = conn.execute("SELECT is_current, generation_start, valid_from FROM logos").fetchone()
        model_columns = {row[1] for row in conn.execute("PRAGMA table_info(models)")}
        old_models = conn.execute("SELECT COUNT(*) FROM models_v4").fetchone()[0]
    assert (logo["is_current"], logo["generation_start"], logo["valid_from"]) == (1, None, "f")
    assert {"library", "feature_version", "accuracy", "rule_accuracy", "is_active", "model_data"} <= model_columns
    assert "target_type" not in model_columns
    assert old_models == 1
    assert db.get_station_logo(4, 211)["recordings_count"] == 2


def test_episodes_of_one_recording_stay_active_and_recut_replaces_only_its_episode(tmp_path):
    """v6(2026-10-11): 保存名称が違い区間が重ならないカットは同じ録画の別の話として両方有効。
    同じ保存名称か、区間が重なるカットはカットし直し(その話の記録だけ無効になる)。"""
    from ts_smartcut.autocut.proposals import keep_ranges

    source = tmp_path / "two_episodes.ts"
    source.write_bytes(b"two episodes")
    db = CutHistoryDB(tmp_path / "cut_history.db")
    recorder = AutoCutRecorder(db)

    def cut(ranges, name):
        info = _file_info(source)
        info.cuts = [CutRange(a, b) for a, b in ranges]
        return recorder.record_batch_done(info, "smart_cut", output_name=name)

    ep1 = cut([(100, 1000)], "第1話.ts")
    ep2 = cut([(1500, 2900)], "第2話.ts")
    ep1_again = cut([(110, 1000)], "第1話.ts")
    ep2_renamed = cut([(1490, 2900)], "第2話_直し.ts")  # 名前が違っても区間が重なればカットし直し

    def active():
        with db.get_connection() as conn:
            return [row[0] for row in conn.execute("SELECT id FROM cut_sessions WHERE is_active = 1 ORDER BY id")]

    assert active() == [ep1_again, ep2_renamed]
    assert ep1 not in active() and ep2 not in active()
    recording_id = db.get_session(ep1)["recording_id"]
    assert keep_ranges(db, recording_id) == [(110, 1000, False), (1490, 2900, False)]
    assert db.get_session(ep2_renamed)["output_name"] == "第2話_直し.ts"


def test_v5_database_reactivates_episodes_that_do_not_overlap(tmp_path):
    """v5 → v6: v5 では2つ目の話を変換すると1つ目の記録が無効になった(CS-Cの
    3本)。区間が有効なカットと重ならない無効なカットは有効に戻す。重なるもの(カットし直し)は
    無効のまま。"""
    path = tmp_path / "v5.db"
    db = CutHistoryDB(path)
    with db.get_connection() as conn:
        conn.execute("DELETE FROM schema_info WHERE version > 5")
        conn.execute("INSERT INTO recordings (id, file_name, file_size, file_hash, created_at) "
                     "VALUES (1, 'a.ts', 10, 'h', 'x')")
        for sess_id, active, ranges in ((1, 0, [(0, 100)]),        # 古いカット(2と重なる)
                                        (2, 0, [(10, 100)]),       # 1つ目の話
                                        (3, 1, [(200, 300)])):     # 2つ目の話
            conn.execute("INSERT INTO cut_sessions (id, recording_id, finalized_at, processing_mode, source_type, "
                         "is_active) VALUES (?, 1, 'x', 'smart_cut', 'manual', ?)", (sess_id, active))
            for a, b in ranges:
                conn.execute("INSERT INTO cut_ranges (session_id, start_frame, end_frame) VALUES (?, ?, ?)",
                             (sess_id, a, b))

    db = CutHistoryDB(path)

    with db.get_connection() as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
        sessions = conn.execute("SELECT id, is_active, output_name FROM cut_sessions ORDER BY id").fetchall()
    assert version == CURRENT_SCHEMA_VERSION
    assert [(s["id"], s["is_active"], s["output_name"]) for s in sessions] == [(1, 0, None), (2, 1, None), (3, 1, None)]


def test_history_db_v8_migrates_to_v9_with_an_empty_imported_logos_table(tmp_path):
    """版 9 (2026-10-15): 取り込んだ局ロゴ (.lgd) の表。移行の時は空。"""
    import sqlite3

    path = tmp_path / "cut_history.db"
    db = CutHistoryDB(path)
    with db.get_connection() as conn:  # 版 8 の DB にする
        conn.execute("DROP TABLE imported_logos;")
        conn.execute("DELETE FROM schema_info WHERE version >= 9;")
    CutHistoryDB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM imported_logos").fetchone()[0] == 0
    conn.close()


def test_history_db_v9_migrates_to_v10_with_audio_columns_on_cut_sessions(tmp_path):
    """版 10 (2026-10-26、設計書 §4.12): カットの記録に選んだ音声。それまでの記録は NULL (最初の音声)。"""
    import sqlite3

    path = tmp_path / "cut_history.db"
    db = CutHistoryDB(path)
    with db.get_connection() as conn:  # 版 9 の DB にする (音声の列を消す)
        for name in ("audio_index", "audio_pid", "audio_component_tag", "audio_component_type", "audio_text"):
            conn.execute(f"ALTER TABLE cut_sessions DROP COLUMN {name};")
        conn.execute("DELETE FROM schema_info WHERE version >= 10;")
        rid = db.insert_recording(conn, {"file_name": "a.ts", "file_size": 1, "file_hash": "h", "created_at": "x"})
        conn.execute("INSERT INTO cut_sessions (recording_id, finalized_at, processing_mode, source_type) "
                     "VALUES (?, 'x', 'smart_cut', 'manual');", (rid,))
    CutHistoryDB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_info").fetchone()[0] == CURRENT_SCHEMA_VERSION == 10
    row = conn.execute("SELECT audio_index, audio_pid, audio_component_tag, audio_text FROM cut_sessions").fetchone()
    assert row == (None, None, None, None)
    conn.close()
