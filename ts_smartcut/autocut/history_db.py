"""カット記録 DB (SQLite) の管理と移行 (設計書 §31.4、自動カット計画 A2)。

設定フォルダに cut_history.db を作成し、録画情報、確定カット区間、
編集操作ログ(edit_events)などを永続化する。
書き込みはバッチスレッドから行われ、WAL モードと適切な接続管理で
GUI スレッドからの参照と競合しないようにする。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from ts_smartcut.core.settings_manager import get_default_settings_path
from ts_smartcut.errors import ProjectError

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 10


def get_default_history_db_path() -> Path:
    """OS標準の設定ディレクトリ配下に cut_history.db を置く。"""
    return get_default_settings_path().with_name("cut_history.db")


class CutHistoryDB:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_default_history_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextlib.contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """スレッドセーフな SQLite 接続を提供するコンテキストマネージャ。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """データベースを初期化し、必要に応じてマイグレーションを行う。"""
        with self.get_connection() as conn:
            current_version = self._get_schema_version(conn)
            if current_version > CURRENT_SCHEMA_VERSION:
                # 新しい版のアプリで作った DB。古い版が書き込むと壊すおそれがあるので
                # 使わない(プロジェクトファイルの未知の版と同じく E403)
                raise ProjectError("E403", key="exceptions.history_db_newer", found=current_version,
                                   supported=CURRENT_SCHEMA_VERSION, path=self.db_path)
            if current_version < CURRENT_SCHEMA_VERSION:
                self._migrate_database(conn, current_version, CURRENT_SCHEMA_VERSION)

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info';"
        )
        if not cur.fetchone():
            return 0
        cur.execute("SELECT MAX(version) FROM schema_info;")
        row = cur.fetchone()
        return row[0] if (row and row[0] is not None) else 0

    def _migrate_database(
        self, conn: sqlite3.Connection, from_version: int, to_version: int
    ) -> None:
        logger.info(
            "Migrating cut_history.db: v%d -> v%d",
            from_version,
            to_version,
        )
        if from_version < 1 <= to_version:
            self._migrate_to_v1(conn)
        if from_version < 2 <= to_version:
            self._migrate_to_v2(conn)
        if from_version < 3 <= to_version:
            self._migrate_to_v3(conn)
        if from_version < 4 <= to_version:
            self._migrate_to_v4(conn)
        if from_version < 5 <= to_version:
            self._migrate_to_v5(conn)
        if from_version < 6 <= to_version:
            self._migrate_to_v6(conn)
        if from_version < 7 <= to_version:
            self._migrate_to_v7(conn)
        if from_version < 8 <= to_version:
            self._migrate_to_v8(conn)
        if from_version < 9 <= to_version:
            self._migrate_to_v9(conn)
        if from_version < 10 <= to_version:
            self._migrate_to_v10(conn)

    def _migrate_to_v1(self, conn: sqlite3.Connection) -> None:
        """スキーマ v1: 初期テーブル群の作成。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                version INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                file_hash TEXT NOT NULL,
                service_id INTEGER,
                original_network_id INTEGER,
                transport_stream_id INTEGER,
                service_name TEXT,
                provider_name TEXT,
                broadcast_type TEXT,
                program_name TEXT,
                genre INTEGER,
                program_description TEXT,
                program_extended TEXT,
                broadcast_start TEXT,
                broadcast_duration INTEGER,
                recording_start TEXT,
                recording_end TEXT,
                video_codec TEXT,
                audio_codec TEXT,
                width INTEGER,
                height INTEGER,
                scan_type TEXT,
                fps REAL,
                bitrate INTEGER,
                total_frames INTEGER,
                created_at TEXT NOT NULL,
                is_replaced INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_recordings_hash_size ON recordings(file_hash, file_size);
            CREATE INDEX IF NOT EXISTS idx_recordings_service ON recordings(service_id);

            CREATE TABLE IF NOT EXISTS cut_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                finalized_at TEXT NOT NULL,
                processing_mode TEXT NOT NULL,
                source_type TEXT NOT NULL,
                app_version TEXT,
                is_active INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_cut_sessions_recording ON cut_sessions(recording_id);

            CREATE TABLE IF NOT EXISTS cut_ranges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES cut_sessions(id) ON DELETE CASCADE,
                start_frame INTEGER NOT NULL,
                end_frame INTEGER NOT NULL,
                label TEXT,
                is_excluded INTEGER DEFAULT 0,
                proposal_start_frame INTEGER,
                proposal_end_frame INTEGER,
                diff_frames INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_cut_ranges_session ON cut_ranges(session_id);

            CREATE TABLE IF NOT EXISTS edit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES cut_sessions(id) ON DELETE CASCADE,
                event_order INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_edit_events_session ON edit_events(session_id);

            CREATE TABLE IF NOT EXISTS features (
                recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
                computed_at TEXT NOT NULL,
                data BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                recordings_count INTEGER DEFAULT 0,
                logo_data BLOB,
                mask_data BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_logos_service ON logos(service_id);

            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,
                service_id INTEGER,
                trained_at TEXT NOT NULL,
                samples_count INTEGER,
                accuracy REAL,
                model_data BLOB
            );
            """
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (1, ?);",
            (now_iso,),
        )

    def _migrate_to_v2(self, conn: sqlite3.Connection) -> None:
        """スキーマ v2 (A3): features に特徴の版・最後まで計算できたか・フレーム数を足す。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(features);")}
        for name, decl in (
            ("feature_version", "INTEGER NOT NULL DEFAULT 0"),
            ("is_complete", "INTEGER NOT NULL DEFAULT 0"),
            ("frame_count", "INTEGER"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE features ADD COLUMN {name} {decl};")
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (2, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v3(self, conn: sqlite3.Connection) -> None:
        """スキーマ v3: 同じ動画(先頭のハッシュとサイズが同じ)は recordings の1行にまとめ、
        カットし直すたびに cut_sessions を足す。recordings.created_at は最初に登録した日時、
        updated_at は最後にカットを確定した日時。

        v2 までは、カットし直すたびに recordings の行を足して古い行に is_replaced=1 を
        付けていた。古い行のカットの記録は新しい行に移し、古い行(とその特徴。特徴は
        カットし直した時に計算し直している)は消す。is_replaced は使わなくなった。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings);")}
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE recordings ADD COLUMN updated_at TEXT;")
        replaced = conn.execute(
            "SELECT id, file_hash, file_size, created_at FROM recordings WHERE is_replaced = 1;"
        ).fetchall()
        for old_id, file_hash, file_size, created_at in replaced:
            row = conn.execute(
                "SELECT id FROM recordings WHERE file_hash = ? AND file_size = ? AND is_replaced = 0 "
                "ORDER BY id DESC LIMIT 1;",
                (file_hash, file_size),
            ).fetchone()
            if row is None:
                continue
            new_id = row[0]
            conn.execute("UPDATE cut_sessions SET recording_id = ? WHERE recording_id = ?;", (new_id, old_id))
            conn.execute(
                "UPDATE recordings SET created_at = MIN(created_at, ?) WHERE id = ?;", (created_at, new_id)
            )
            conn.execute("DELETE FROM features WHERE recording_id = ?;", (old_id,))
            conn.execute("DELETE FROM recordings WHERE id = ?;", (old_id,))
        # 最後にカットを確定した日時
        conn.execute(
            """
            UPDATE recordings SET updated_at = COALESCE(
                (SELECT MAX(finalized_at) FROM cut_sessions WHERE recording_id = recordings.id), created_at
            ) WHERE updated_at IS NULL;
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (3, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v4(self, conn: sqlite3.Connection) -> None:
        """スキーマ v4 (A4): 局のロゴの型。局は original_network_id と service_id で決める
        (service_id だけだと地上波の地域・BS・CS で重なりうる)。logo_data は型
        (logo.template_to_blob)、mask_data は使わない。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(logos);")}
        for name, decl in (("original_network_id", "INTEGER"), ("updated_at", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE logos ADD COLUMN {name} {decl};")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logos_station ON logos(original_network_id, service_id);")
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (4, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v5(self, conn: sqlite3.Connection) -> None:
        """スキーマ v5 (2026-10-09 の決定、設計書 §31.9):

        - logos: 局ごとに型を複数持てるようにする。is_current=1 がその局の今の型で、ロゴが
          変わって作り直した時(A9)は古い型を is_current=0 にして期間(valid_from・valid_to)付きで
          残す。generation_start は今の型を作る録画の始まり(録画の日時。これより前の録画は
          使わない。NULL はすべて)。今までの行は今の型。
        - models: 局ごとではなく、局を特徴の1つにした1つのモデル(A8)。今まで書き込む処理は
          無かったので作り直す(行があれば models_v4 に名前を変えて残す)。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(logos);")}
        if "is_current" not in columns:
            conn.execute("ALTER TABLE logos ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1;")
        if "generation_start" not in columns:
            conn.execute("ALTER TABLE logos ADD COLUMN generation_start TEXT;")
        count = conn.execute("SELECT COUNT(*) FROM models;").fetchone()[0]
        if count:
            conn.execute("ALTER TABLE models RENAME TO models_v4;")
        else:
            conn.execute("DROP TABLE models;")
        conn.executescript(
            """
            CREATE TABLE models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trained_at TEXT NOT NULL,
                library TEXT NOT NULL,            -- 学習のライブラリ(lightgbm 等)
                library_version TEXT,
                feature_version INTEGER,          -- 学習に使った特徴の版(features.FEATURE_VERSION)
                recordings_count INTEGER,         -- 学習に使った録画の数
                samples_count INTEGER,            -- 学習に使った区間の数
                eval_recordings_count INTEGER,    -- 確認用(学習に使わない)録画の数
                accuracy REAL,                    -- 確認用の録画での一致率
                rule_accuracy REAL,               -- 同じ確認用の録画でのルールの一致率
                details TEXT,                     -- 局ごとの一致率などの JSON
                is_active INTEGER NOT NULL DEFAULT 0,  -- 提案に使うモデル(ルールより良い時だけ 1)
                model_data BLOB
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (5, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v6(self, conn: sqlite3.Connection) -> None:
        """スキーマ v6 (2026-10-11 利用者の指示): 1本の録画に複数の話が入っていて、話ごとに
        分けて変換する。cut_sessions に保存名称(output_name、出力ファイルの名前)を足し、
        同じ録画でも保存名称が違い、残す区間が重ならないカットは別の話として両方を有効にする
        (record_batch_done)。その録画の正解は有効なカットすべての区間を合わせたもの。

        v5 までは2つ目の話を変換すると1つ目の記録が無効(is_active=0)になった(CS-C
        の3本)。無効なカットのうち、区間がその録画の有効なカットのどれとも重ならないものは
        分けて変換した話なので有効に戻す。今までの行の保存名称は分からないので空(NULL)。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(cut_sessions);")}
        if "output_name" not in columns:
            conn.execute("ALTER TABLE cut_sessions ADD COLUMN output_name TEXT;")
        recordings = [row[0] for row in conn.execute(
            "SELECT recording_id FROM cut_sessions GROUP BY recording_id HAVING COUNT(*) > 1;")]
        for recording_id in recordings:
            sessions = conn.execute(
                "SELECT id, is_active FROM cut_sessions WHERE recording_id = ? ORDER BY id DESC;", (recording_id,)
            ).fetchall()
            kept = [s[0] for s in sessions if s[1]]
            for session_id, active in sessions:  # 新しい順。戻したカットとも重ならないものだけ
                if active:
                    continue
                ranges = _session_ranges(conn, session_id)
                if ranges and not any(_overlaps(ranges, _session_ranges(conn, k)) for k in kept):
                    conn.execute("UPDATE cut_sessions SET is_active = 1 WHERE id = ?;", (session_id,))
                    kept.append(session_id)
                    logger.info("Reactivated the cut of a separately converted episode: recording_id=%d, session_id=%d",
                                recording_id, session_id)
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (6, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v7(self, conn: sqlite3.Connection) -> None:
        """スキーマ v7 (2026-10-13 利用者の指示、設計書 §31.9「局のロゴの書き出し」): 局のロゴ画像
        (.lgd)の材料を録画ごとに保存する logo_samples を作る(logo_image.py)。材料は特徴の計算の
        読み込みの時にしか取れないので、書き出し(A9 の後)より先に集め始める。今までの録画には無い。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS logo_samples (
                recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
                computed_at TEXT NOT NULL,
                samples_version INTEGER NOT NULL,  -- logo_image.SAMPLES_VERSION
                pictures INTEGER,                  -- 使った残した区間のキーフレームの数
                data BLOB NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (7, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v8(self, conn: sqlite3.Connection) -> None:
        """スキーマ v8 (2026-10-14 利用者の指示で前倒し、設計書 §31.9「ルールのしきい値」): 判定の
        しきい値を局ごと・全局共通で持つ thresholds を作る(thresholds.py)。行は手で直した時か
        自動補正を採用した時だけ作るので、移行の時は空(全局が初期値のまま)。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS thresholds (
                station_key TEXT NOT NULL,        -- '' = 全局共通、ほかは '<original_network_id>:<service_id>'
                original_network_id INTEGER,
                service_id INTEGER,
                name TEXT NOT NULL,               -- thresholds.Thresholds の項目
                value REAL NOT NULL,
                source TEXT NOT NULL,             -- manual(手で直した) / auto(自動補正を採用した)
                updated_at TEXT NOT NULL,
                PRIMARY KEY (station_key, name)
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (8, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v9(self, conn: sqlite3.Connection) -> None:
        """スキーマ v9 (2026-10-15 利用者の決定、設計書 §31.9「局ロゴの取り込み」): エンコードソフトの
        局ロゴ画像 (.lgd) から作った型を imported_logos に取り込む (lgd_import.py。Logo/ フォルダから自動)。
        局の型がまだ無い局で、カットする前の録画の一致度に使う。移行の時は空。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS imported_logos (
                service_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,          -- 取り込んだ .lgd の名前 (SID<番号>-<n>.lgd)
                logo_name TEXT,                   -- .lgd の中のロゴの名前
                logo_date TEXT,                   -- 名前の末尾の日付 (YYYY-MM-DD)。無ければファイルの更新日
                image_width INTEGER NOT NULL,     -- 作った時の映像の大きさ
                image_height INTEGER NOT NULL,
                logo_data BLOB NOT NULL,          -- logo.template_to_blob の型
                lgd_data BLOB NOT NULL,           -- .lgd そのもの (作り方を変えた時に作り直す・書き出しの確かめ)
                file_mtime_ns INTEGER NOT NULL,   -- 取り込んだ時のファイルの更新日時・大きさ (変わったら取り込み直す)
                file_size INTEGER NOT NULL,
                stale_notified INTEGER NOT NULL DEFAULT 0,  -- 古い (録画のロゴと合わない) と知らせた
                imported_at TEXT NOT NULL,
                PRIMARY KEY (service_id, file_name)
            );

            -- 局のロゴが変わったかの知らせ (A9) で「いいえ」と答えた時の、その時の最新の録画の日時。
            -- これより後の録画が続けて合わなくなるまで聞かない (logo_change.py)
            CREATE TABLE IF NOT EXISTS logo_change_dismissals (
                station_key TEXT PRIMARY KEY,     -- '<original_network_id>:<service_id>'
                dismissed_since TEXT NOT NULL,    -- 合わなかった最初の録画の日時 (聞き直す時はこれより前の録画と比べる)
                dismissed_through TEXT NOT NULL,  -- 合わなかった最新の録画の日時 (これより後に 3 本たまるまで聞かない)
                dismissed_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (9, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def _migrate_to_v10(self, conn: sqlite3.Connection) -> None:
        """スキーマ v10 (2026-10-26 利用者の決定、設計書 §4.12): カットの記録に、出力に使った(選んだ)音声を
        残す。主な鍵は component_tag (同じ番組の中で PID が入れ替わる録画がある)。それまでの記録は NULL
        (最初の音声、記録なし)。自動カットの案と特徴は選んだ音声に関係なく最初の音声で作る。"""
        for name, decl in (
            ("audio_index", "INTEGER"),           # 選べる音声の一覧の何番目か (0 = 最初の音声)
            ("audio_pid", "INTEGER"),
            ("audio_component_tag", "INTEGER"),
            ("audio_component_type", "INTEGER"),  # 0x03 ステレオ・0x02 デュアルモノ等 (EIT 0xC4)
            ("audio_text", "TEXT"),               # 放送局が入れた説明 (「解説」等)
        ):
            if name not in {r[1] for r in conn.execute("PRAGMA table_info(cut_sessions);")}:
                conn.execute(f"ALTER TABLE cut_sessions ADD COLUMN {name} {decl};")
        conn.execute(
            "INSERT OR REPLACE INTO schema_info (version, updated_at) VALUES (10, ?);",
            (datetime.now(timezone.utc).isoformat(),),
        )

    # ---- CRUD / クエリ ヘルパー ----------------------------------------

    def find_recording_by_hash(
        self, conn: sqlite3.Connection, file_hash: str, file_size: int
    ) -> Optional[sqlite3.Row]:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM recordings
            WHERE file_hash = ? AND file_size = ?
            ORDER BY id DESC LIMIT 1;
            """,
            (file_hash, file_size),
        )
        return cur.fetchone()

    def insert_recording(
        self, conn: sqlite3.Connection, rec_data: Dict[str, Any]
    ) -> int:
        fields = [
            "file_name",
            "file_size",
            "file_hash",
            "service_id",
            "original_network_id",
            "transport_stream_id",
            "service_name",
            "provider_name",
            "broadcast_type",
            "program_name",
            "genre",
            "program_description",
            "program_extended",
            "broadcast_start",
            "broadcast_duration",
            "recording_start",
            "recording_end",
            "video_codec",
            "audio_codec",
            "width",
            "height",
            "scan_type",
            "fps",
            "bitrate",
            "total_frames",
            "created_at",
            "updated_at",
        ]
        placeholders = ", ".join("?" for _ in fields)
        cols = ", ".join(fields)
        values = [rec_data.get(f) for f in fields]
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO recordings ({cols}) VALUES ({placeholders});", values
        )
        return cur.lastrowid

    def update_recording(self, conn: sqlite3.Connection, recording_id: int, rec_data: Dict[str, Any]) -> None:
        """同じ動画をカットし直した時に、録画の情報(番組情報等)と updated_at を新しくする。
        created_at(最初に登録した日時)は変えない。"""
        fields = [f for f in rec_data if f not in ("id", "created_at", "is_replaced")]
        if not fields:
            return
        assignments = ", ".join(f"{f} = ?" for f in fields)
        conn.execute(
            f"UPDATE recordings SET {assignments} WHERE id = ?;",
            [rec_data[f] for f in fields] + [recording_id],
        )

    def insert_session(
        self, conn: sqlite3.Connection, session_data: Dict[str, Any]
    ) -> int:
        fields = [
            "recording_id",
            "finalized_at",
            "processing_mode",
            "source_type",
            "app_version",
            "is_active",
            "output_name",
            "audio_index",
            "audio_pid",
            "audio_component_tag",
            "audio_component_type",
            "audio_text",
        ]
        placeholders = ", ".join("?" for _ in fields)
        cols = ", ".join(fields)
        values = [session_data.get(f) for f in fields]
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO cut_sessions ({cols}) VALUES ({placeholders});", values
        )
        return cur.lastrowid

    def deactivate_recording_sessions(
        self, conn: sqlite3.Connection, recording_id: int
    ) -> None:
        conn.execute(
            "UPDATE cut_sessions SET is_active = 0 WHERE recording_id = ?;",
            (recording_id,),
        )

    def deactivate_replaced_sessions(
        self, conn: sqlite3.Connection, recording_id: int, output_name: Optional[str], ranges: List[tuple]
    ) -> List[int]:
        """カットし直しで置き換わる有効なカットを無効にし、その id を返す。置き換わるのは、
        保存名称が同じカット・保存名称が無い(v6 より前か、名称を渡さない呼び出し)カット同士・
        残す区間(ranges: (開始, 終了))が重なるカット。どれでもないカットは同じ録画の別の話。"""
        replaced = []
        for row in conn.execute(
            "SELECT id, output_name FROM cut_sessions WHERE recording_id = ? AND is_active = 1;", (recording_id,)
        ).fetchall():
            same_name = row["output_name"] == output_name if output_name else not row["output_name"]
            if same_name or _overlaps(ranges, _session_ranges(conn, row["id"])):
                replaced.append(row["id"])
        for session_id in replaced:
            conn.execute("UPDATE cut_sessions SET is_active = 0 WHERE id = ?;", (session_id,))
        return replaced

    def insert_cut_ranges(
        self, conn: sqlite3.Connection, session_id: int, ranges: List[Dict[str, Any]]
    ) -> None:
        cur = conn.cursor()
        for r in ranges:
            cur.execute(
                """
                INSERT INTO cut_ranges (
                    session_id, start_frame, end_frame, label, is_excluded,
                    proposal_start_frame, proposal_end_frame, diff_frames
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    session_id,
                    r["start_frame"],
                    r["end_frame"],
                    r.get("label"),
                    1 if r.get("is_excluded") else 0,
                    r.get("proposal_start_frame"),
                    r.get("proposal_end_frame"),
                    r.get("diff_frames"),
                ),
            )

    def insert_edit_events(
        self, conn: sqlite3.Connection, session_id: int, events: List[Dict[str, Any]]
    ) -> None:
        cur = conn.cursor()
        for ev in events:
            details_str = json.dumps(ev.get("details", {}), ensure_ascii=False)
            cur.execute(
                """
                INSERT INTO edit_events (
                    session_id, event_order, event_type, details, created_at
                ) VALUES (?, ?, ?, ?, ?);
                """,
                (
                    session_id,
                    ev.get("event_order", 0),
                    ev.get("event_type", "unknown"),
                    details_str,
                    ev.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )

    def get_video_registration(self, file_hash: str, file_size: int) -> Optional[Dict[str, Any]]:
        """登録済みの動画なら、id・登録日時(created_at)・最後にカットを確定した日時
        (updated_at)・カットの回数(cut_count)。未登録なら None。"""
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT r.id, r.created_at, r.updated_at,
                       (SELECT COUNT(*) FROM cut_sessions s WHERE s.recording_id = r.id) AS cut_count
                FROM recordings r
                WHERE r.file_hash = ? AND r.file_size = ?
                ORDER BY r.id DESC LIMIT 1;
                """,
                (file_hash, file_size),
            ).fetchone()
            return dict(row) if row else None

    def get_recording(self, recording_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM recordings WHERE id = ?;", (recording_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_active_session_for_recording(
        self, recording_id: int
    ) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM cut_sessions
                WHERE recording_id = ? AND is_active = 1
                ORDER BY id DESC LIMIT 1;
                """,
                (recording_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_active_cut_ranges(self, recording_id: int) -> List[Dict[str, Any]]:
        """録画の正解の残す区間: 有効なカットすべての区間(話ごとに分けて変換した録画は
        複数のカットが有効、v6)。開始の順。"""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.* FROM cut_ranges c JOIN cut_sessions s ON s.id = c.session_id
                WHERE s.recording_id = ? AND s.is_active = 1
                ORDER BY c.start_frame ASC, c.id ASC;
                """,
                (recording_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session_cut_ranges(self, session_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM cut_ranges
                WHERE session_id = ?
                ORDER BY start_frame ASC;
                """,
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_session_edit_events(self, session_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM edit_events
                WHERE session_id = ?
                ORDER BY event_order ASC;
                """,
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM cut_sessions WHERE id = ?;", (session_id,)).fetchone()
            return dict(row) if row else None

    # ---- 特徴 (A3) ------------------------------------------------------

    def save_features(
        self, recording_id: int, blob: bytes, feature_version: int, is_complete: bool, frame_count: int
    ) -> None:
        """録画の特徴を保存する(既にあれば置き換える)。"""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO features (
                    recording_id, computed_at, data, feature_version, is_complete, frame_count
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    recording_id,
                    datetime.now(timezone.utc).isoformat(),
                    sqlite3.Binary(blob),
                    feature_version,
                    1 if is_complete else 0,
                    frame_count,
                ),
            )

    def get_features_row(self, recording_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM features WHERE recording_id = ?;", (recording_id,)).fetchone()
            return dict(row) if row else None

    # ---- 局のロゴ画像の材料 (.lgd、設計書 §31.9) ----------------------------

    def save_logo_samples(self, recording_id: int, blob: bytes, samples_version: int, pictures: int) -> None:
        """録画のロゴ画像の材料を保存する(既にあれば置き換える)。"""
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO logo_samples (recording_id, computed_at, samples_version, pictures, data) "
                "VALUES (?, ?, ?, ?, ?);",
                (recording_id, datetime.now(timezone.utc).isoformat(), samples_version, pictures,
                 sqlite3.Binary(blob)),
            )

    def get_station_logo_samples(
        self, original_network_id: Optional[int], service_id: int
    ) -> List[Dict[str, Any]]:
        """局の録画のロゴ画像の材料を、録画の日時の順に(recorded_at は get_station_feature_blobs と同じ)。"""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.id AS recording_id,
                       COALESCE(r.recording_start, r.broadcast_start, r.created_at) AS recorded_at,
                       s.samples_version, s.pictures, s.data
                FROM recordings r JOIN logo_samples s ON s.recording_id = r.id
                WHERE r.service_id = ? AND r.original_network_id IS ?
                ORDER BY recorded_at ASC, r.id ASC;
                """,
                (service_id, original_network_id),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- 局のロゴの型 (A4) ------------------------------------------------

    def get_station_feature_blobs(
        self, original_network_id: Optional[int], service_id: int, since: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """局の録画の特徴(版 2 以降で最後まで計算したもの。ロゴの型がある)を、録画の日時の順に。
        recorded_at は録画開始(無ければ放送開始、登録日時)。since があれば、その日時以降の録画だけ。"""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT r.id AS recording_id,
                           COALESCE(r.recording_start, r.broadcast_start, r.created_at) AS recorded_at,
                           f.data AS data
                    FROM recordings r JOIN features f ON f.recording_id = r.id
                    WHERE r.service_id = ? AND r.original_network_id IS ? AND f.feature_version >= 2
                          AND f.is_complete = 1
                )
                WHERE ? IS NULL OR recorded_at >= ?
                ORDER BY recorded_at ASC, recording_id ASC;
                """,
                (service_id, original_network_id, since, since),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_station_logo(
        self,
        original_network_id: Optional[int],
        service_id: int,
        logo_blob: Optional[bytes],
        recordings_count: int,
        valid_from: Optional[str],
        valid_to: Optional[str],
    ) -> None:
        """局の今の型(is_current=1)を置き換える。logo_blob が None なら型を消す(型を作れなく
        なった)。作り直しの起点(generation_start)のある行は、起点を覚えておくために行を残して
        型だけ消す。古い型(is_current=0)には触らない。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT id, generation_start FROM logos WHERE service_id = ? AND original_network_id IS ? "
                "AND is_current = 1 ORDER BY id DESC LIMIT 1;",
                (service_id, original_network_id),
            ).fetchone()
            if logo_blob is None:
                if row is not None and row["generation_start"] is not None:
                    conn.execute(
                        "UPDATE logos SET updated_at = ?, valid_from = NULL, valid_to = NULL, recordings_count = 0, "
                        "logo_data = NULL WHERE id = ?;",
                        (now, row["id"]),
                    )
                elif row is not None:
                    conn.execute("DELETE FROM logos WHERE id = ?;", (row["id"],))
                return
            if row is None:
                conn.execute(
                    """
                    INSERT INTO logos (service_id, original_network_id, created_at, updated_at,
                                       valid_from, valid_to, recordings_count, logo_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (service_id, original_network_id, now, now, valid_from, valid_to, recordings_count,
                     sqlite3.Binary(logo_blob)),
                )
            else:
                conn.execute(
                    """
                    UPDATE logos SET updated_at = ?, valid_from = ?, valid_to = ?, recordings_count = ?,
                                     logo_data = ?
                    WHERE id = ?;
                    """,
                    (now, valid_from, valid_to, recordings_count, sqlite3.Binary(logo_blob), row["id"]),
                )

    def get_station_logo(
        self, original_network_id: Optional[int], service_id: int, recorded_at: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """局の今の型の行(型が無い、作り直しの起点だけの行もある)。recorded_at を渡すと、その
        日時が期間に入る古い型(ロゴが変わる前の録画)があればそれを返す。"""
        with self.get_connection() as conn:
            if recorded_at is not None:
                row = conn.execute(
                    "SELECT * FROM logos WHERE service_id = ? AND original_network_id IS ? AND is_current = 0 "
                    "AND logo_data IS NOT NULL AND valid_from <= ? AND ? <= valid_to ORDER BY id DESC LIMIT 1;",
                    (service_id, original_network_id, recorded_at, recorded_at),
                ).fetchone()
                if row is not None:
                    return dict(row)
            row = conn.execute(
                "SELECT * FROM logos WHERE service_id = ? AND original_network_id IS ? AND is_current = 1 "
                "ORDER BY id DESC LIMIT 1;",
                (service_id, original_network_id),
            ).fetchone()
            return dict(row) if row else None

    def start_station_logo_generation(
        self, original_network_id: Optional[int], service_id: int, since: str
    ) -> None:
        """ロゴが変わった時(A9 の「はい」): 今の型を古い型(is_current=0、期間はそのまま)にして、
        since(録画の日時)以降の録画だけで作る新しい今の型の行を作る(型はまだ無い)。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE logos SET is_current = 0, updated_at = ? WHERE service_id = ? AND original_network_id IS ? "
                "AND is_current = 1;",
                (now, service_id, original_network_id),
            )
            # 型の無かった行(起点だけ)は古い型として残す意味が無い
            conn.execute(
                "DELETE FROM logos WHERE service_id = ? AND original_network_id IS ? AND is_current = 0 "
                "AND logo_data IS NULL;",
                (service_id, original_network_id),
            )
            conn.execute(
                """
                INSERT INTO logos (service_id, original_network_id, created_at, updated_at, recordings_count,
                                   is_current, generation_start)
                VALUES (?, ?, ?, ?, 0, 1, ?);
                """,
                (service_id, original_network_id, now, now, since),
            )

    def get_threshold_rows(self) -> Dict[str, Dict[str, Any]]:
        """しきい値の行すべて: 局の鍵('' = 全局共通) → 項目 → ThresholdRow。"""
        from ts_smartcut.autocut.thresholds import ThresholdRow

        result: Dict[str, Dict[str, Any]] = {}
        with self.get_connection() as conn:
            for r in conn.execute("SELECT station_key, name, value, source FROM thresholds;"):
                result.setdefault(r["station_key"], {})[r["name"]] = ThresholdRow(float(r["value"]), r["source"])
        return result

    def get_threshold_times(self) -> Dict[tuple, str]:
        """しきい値の行を書いた日時(UTC の ISO 形式): (局の鍵, 項目) → 日時。"""
        with self.get_connection() as conn:
            return {(r["station_key"], r["name"]): r["updated_at"]
                    for r in conn.execute("SELECT station_key, name, updated_at FROM thresholds;")}

    def save_threshold_changes(self, changes: Dict[tuple, Optional[Any]]) -> None:
        """しきい値の行をまとめて書く。changes: (局の鍵, 項目) → ThresholdRow(None は行を消す=
        下の段の値に戻す)。局の鍵から original_network_id・service_id も入れる(読む人のため)。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            for (key, name), row in changes.items():
                if row is None:
                    conn.execute("DELETE FROM thresholds WHERE station_key = ? AND name = ?;", (key, name))
                    continue
                onid = sid = None
                if key:
                    onid_text, sid_text = key.split(":")
                    onid = int(onid_text) if onid_text else None
                    sid = int(sid_text)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO thresholds
                        (station_key, original_network_id, service_id, name, value, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (key, onid, sid, name, float(row.value), row.source, now),
                )

    # ---- 学習のモデル (A8、設計書 §31.9) --------------------------------

    def save_model(self, library: str, library_version: str, feature_version: int, recordings_count: int,
                   samples_count: int, eval_recordings_count: int, accuracy: Optional[float],
                   rule_accuracy: Optional[float], details: str, is_active: bool,
                   model_data: Optional[bytes]) -> int:
        """学習の結果を 1 行足す。is_active なら、ほかの行の印を外す(使うモデルは 1 つ)。前の結果は
        残す(学習し直すたびに 1 行。クリアで全部消える)。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            if is_active:
                conn.execute("UPDATE models SET is_active = 0;")
            cur = conn.execute(
                """
                INSERT INTO models (trained_at, library, library_version, feature_version, recordings_count,
                                    samples_count, eval_recordings_count, accuracy, rule_accuracy, details,
                                    is_active, model_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (now, library, library_version, feature_version, recordings_count, samples_count,
                 eval_recordings_count, accuracy, rule_accuracy, details, 1 if is_active else 0, model_data),
            )
            return int(cur.lastrowid)

    def get_latest_model(self, active_only: bool = False, with_data: bool = False) -> Optional[Dict[str, Any]]:
        """いちばん新しい学習の結果(active_only なら使う印のあるもの)。with_data でモデルの BLOB も。"""
        columns = "*" if with_data else (
            "id, trained_at, library, library_version, feature_version, recordings_count, samples_count, "
            "eval_recordings_count, accuracy, rule_accuracy, details, is_active")
        where = "WHERE is_active = 1" if active_only else ""
        with self.get_connection() as conn:
            row = conn.execute(f"SELECT {columns} FROM models {where} ORDER BY id DESC LIMIT 1;").fetchone()
            return dict(row) if row else None

    def clear_models(self) -> int:
        """学習の結果をすべて消す(記録そのものは消さない)。消した行の数。"""
        with self.get_connection() as conn:
            return conn.execute("DELETE FROM models;").rowcount

    def count_recordings(self) -> int:
        """記録 DB の録画の本数(有効なカットのあるもの)。"""
        with self.get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(DISTINCT recording_id) FROM cut_sessions WHERE is_active = 1;"
            ).fetchone()[0]

    def list_stations(self) -> List[Dict[str, Any]]:
        """カットを記録した局(original_network_id, service_id, 局名 = 最新の録画の局名,
        recordings = 最後まで計算した特徴と有効なカットのある録画の本数)。局名の順。"""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.original_network_id, r.service_id,
                       (SELECT r2.service_name FROM recordings r2
                        WHERE r2.service_id = r.service_id AND r2.original_network_id IS r.original_network_id
                        ORDER BY r2.id DESC LIMIT 1) AS service_name,
                       COUNT(DISTINCT r.id) AS recordings
                FROM recordings r
                JOIN features f ON f.recording_id = r.id AND f.feature_version >= 2 AND f.is_complete = 1
                JOIN cut_sessions s ON s.recording_id = r.id AND s.is_active = 1
                WHERE r.service_id IS NOT NULL
                GROUP BY r.original_network_id, r.service_id
                ORDER BY service_name;
                """
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- 取り込んだ局ロゴ (.lgd、設計書 §31.9) --------------------------------

    def save_imported_logo(self, service_id: int, file_name: str, logo_name: Optional[str],
                           logo_date: Optional[str], image_width: int, image_height: int,
                           logo_data: bytes, lgd_data: bytes, file_mtime_ns: int, file_size: int) -> None:
        """取り込んだロゴを保存する (同じファイル名なら置き換える。古いと知らせたことも消える)。"""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM imported_logos WHERE file_name = ?;", (file_name,))
            conn.execute(
                "INSERT INTO imported_logos (service_id, file_name, logo_name, logo_date, image_width, "
                "image_height, logo_data, lgd_data, file_mtime_ns, file_size, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (service_id, file_name, logo_name, logo_date, image_width, image_height,
                 sqlite3.Binary(logo_data), sqlite3.Binary(lgd_data), file_mtime_ns, file_size,
                 datetime.now(timezone.utc).isoformat()),
            )

    def delete_imported_logo(self, file_name: str) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM imported_logos WHERE file_name = ?;", (file_name,))

    def mark_imported_logo_stale_notified(self, file_name: str) -> None:
        with self.get_connection() as conn:
            conn.execute("UPDATE imported_logos SET stale_notified = 1 WHERE file_name = ?;", (file_name,))

    def get_imported_logo(self, service_id: int) -> Optional[Dict[str, Any]]:
        """局の取り込んだロゴのうち、いちばん新しいもの (日付の順、同じならファイル名の順)。"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM imported_logos WHERE service_id = ? "
                "ORDER BY logo_date DESC, file_name DESC LIMIT 1;",
                (service_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_imported_logos(self) -> List[Dict[str, Any]]:
        """取り込んだロゴすべて (型・.lgd の中身は除く)。局の番号の順。"""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT service_id, file_name, logo_name, logo_date, image_width, image_height, file_mtime_ns, "
                "file_size, stale_notified, imported_at FROM imported_logos ORDER BY service_id, logo_date, file_name;"
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- ロゴの変化の知らせ (A9) ----------------------------------------------

    @staticmethod
    def _station_key(original_network_id: Optional[int], service_id: int) -> str:
        return f"{'' if original_network_id is None else original_network_id}:{service_id}"

    def get_logo_change_dismissal(
        self, original_network_id: Optional[int], service_id: int
    ) -> Optional[Tuple[str, str]]:
        """「いいえ」と答えた時の (合わなかった最初の録画の日時, 最新の録画の日時)。無ければ None。"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT dismissed_since, dismissed_through FROM logo_change_dismissals WHERE station_key = ?;",
                (self._station_key(original_network_id, service_id),)).fetchone()
            return (row[0], row[1]) if row else None

    def save_logo_change_dismissal(self, original_network_id: Optional[int], service_id: int,
                                   since: str, through: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO logo_change_dismissals (station_key, dismissed_since, dismissed_through, "
                "dismissed_at) VALUES (?, ?, ?, ?);",
                (self._station_key(original_network_id, service_id), since, through,
                 datetime.now(timezone.utc).isoformat()),
            )

    def clear_logo_change_dismissal(self, original_network_id: Optional[int], service_id: int) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM logo_change_dismissals WHERE station_key = ?;",
                         (self._station_key(original_network_id, service_id),))

    def list_station_logos(self) -> List[Dict[str, Any]]:
        """局の型すべて(古い型も。is_current で見分ける。局名は、その局の最新の録画の局名)。"""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT l.*, (SELECT r.service_name FROM recordings r
                             WHERE r.service_id = l.service_id AND r.original_network_id IS l.original_network_id
                             ORDER BY r.id DESC LIMIT 1) AS service_name
                FROM logos l ORDER BY l.original_network_id, l.service_id, l.is_current, l.id;
                """
            ).fetchall()
            return [dict(r) for r in rows]


def _session_ranges(conn: sqlite3.Connection, session_id: int) -> List[tuple]:
    """カットの残す区間(基準外を除く) (開始, 終了)。"""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT start_frame, end_frame FROM cut_ranges WHERE session_id = ? AND is_excluded = 0;", (session_id,))]


def _overlaps(a: List[tuple], b: List[tuple]) -> bool:
    """区間どうしが1フレームでも重なるか(終わりのフレームが次の始まりと同じだけなら重ならない)。"""
    return any(min(e1, e2) > max(s1, s2) for s1, e1 in a for s2, e2 in b)
