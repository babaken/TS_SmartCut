"""カット確定時の記録 (設計書 §31.4、自動カット計画 A2)。

バッチ処理が録画を Done で終えた時に呼び出され、
録画メタデータ、番組情報(SI)、確定カット区間(残す区間)、
および編集操作ログ(edit_events)を cut_history.db に保存する。

自動カット処理の成否がバッチ処理本体を止めないよう、
すべての例外は内部で捕捉・ログ記録される。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ts_smartcut import __version__
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.broadcast.program_info import read_program_info
from ts_smartcut.models import FileCutInfo

logger = logging.getLogger(__name__)

# 同じ録画の判定に使う先頭ブロックサイズ (16MB)
HASH_PREFIX_BYTES = 16 * 1024 * 1024


def compute_file_hash(path: Path, max_bytes: int = HASH_PREFIX_BYTES) -> str:
    """ファイルの先頭部分の SHA-256 ハッシュを計算する。"""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        data = f.read(max_bytes)
        hasher.update(data)
    return hasher.hexdigest()


def _proposal_fields(start: int, end: int, accepted: List[Dict[str, Any]]) -> Dict[str, Any]:
    """残す区間と最も重なる承認した案と、直したフレーム数(始まりと終わりの差の合計)。"""
    best, best_overlap = None, 0
    for p in accepted:
        overlap = min(end, p["end_frame"]) - max(start, p["start_frame"])
        if overlap > best_overlap:
            best, best_overlap = p, overlap
    if best is None:
        return {}
    return {
        "proposal_start_frame": best["start_frame"],
        "proposal_end_frame": best["end_frame"],
        "diff_frames": abs(start - best["start_frame"]) + abs(end - best["end_frame"]),
    }


def _source_type(cuts, accepted: List[Dict[str, Any]]) -> str:
    if not accepted:
        return "manual"
    exact = {(p["start_frame"], p["end_frame"]) for p in accepted}
    if cuts and all((c.start_frame, c.end_frame) in exact for c in cuts) and len(cuts) == len(exact):
        return "proposal"
    return "proposal_modified"


class AutoCutRecorder:
    def __init__(self, db: Optional[CutHistoryDB] = None):
        self.db = db or CutHistoryDB()

    def record_batch_done(
        self,
        file_info: FileCutInfo,
        processing_mode: str,
        program_info: Optional[Any] = None,
        edit_events: Optional[List[Dict[str, Any]]] = None,
        source_type: Optional[str] = None,
        proposals: Optional[List[Dict[str, Any]]] = None,
        output_name: Optional[str] = None,
    ) -> Optional[int]:
        """バッチ Done 完了時に呼び出され、結果を DB に記録する。

        proposals は自動カットの案(CutProposal.to_dict()、A6)。残す区間ごとに、重なる
        承認した案と、何フレーム直したかを残す。source_type(カットの出どころ)は省略すると
        案から決める: 承認した案が無い = manual、残す区間がすべて承認した案のまま = proposal、
        それ以外 = proposal_modified。

        output_name は保存名称(出力ファイルの名前、v6)。登録済みの録画では、保存名称が同じか
        残す区間が重なる有効なカットだけをカットし直しとして無効にする。それ以外は同じ録画の
        別の話(1本に複数の話が入った録画を話ごとに変換した、2026-10-11)として有効のまま残す。

        戻り値: 成功時は作成された session_id、失敗時は None。
        """
        path = file_info.file_path
        if not path.is_file():
            logger.warning("Not recording the cut because the recording file is missing: %s", path)
            return None

        try:
            file_size = path.stat().st_size
            file_hash = compute_file_hash(path)
        except OSError as e:
            logger.warning("Could not compute the file hash: %s (%s)", path, e)
            return None

        # 録画を開いた直後(裏での番組情報の読み込みが終わる前)にバッチへ追加すると
        # 番組情報が無いので、ここ(バッチのスレッド)で読み直す
        if program_info is None:
            try:
                program_info = read_program_info(path)
            except Exception:
                logger.warning("Could not read the programme information: %s", path, exc_info=True)

        accepted = [p for p in (proposals or []) if p.get("status") == "accepted"]
        try:
            with self.db.get_connection() as conn:
                # 同じ動画(先頭のハッシュとサイズが一致)が登録済みなら、その行を使う
                existing = self.db.find_recording_by_hash(conn, file_hash, file_size)

                # 番組情報 (ProgramInfo) の抽出
                service_id = None
                original_network_id = None
                transport_stream_id = None
                service_name = ""
                provider_name = ""
                broadcast_type = ""
                program_name = ""
                genre = None
                description = ""
                extended = ""
                broadcast_start = None
                broadcast_duration = None
                recording_start = None
                recording_end = None

                if program_info is not None:
                    service_id = getattr(program_info, "service_id", None)
                    original_network_id = getattr(program_info, "original_network_id", None)
                    transport_stream_id = getattr(program_info, "transport_stream_id", None)
                    service_name = getattr(program_info, "service_name", "") or ""
                    provider_name = getattr(program_info, "provider_name", "") or ""
                    broadcast_type = getattr(program_info, "broadcast_type", "") or ""
                    rec_s = getattr(program_info, "recording_start", None)
                    if rec_s:
                        recording_start = rec_s.isoformat()
                    rec_e = getattr(program_info, "recording_end", None)
                    if rec_e:
                        recording_end = rec_e.isoformat()

                    event = getattr(program_info, "event", None)
                    if event is not None:
                        program_name = getattr(event, "name", "") or ""
                        description = getattr(event, "description", "") or ""
                        extended = getattr(event, "extended", "") or ""
                        if event.start:
                            broadcast_start = event.start.isoformat()
                        if event.duration:
                            broadcast_duration = int(event.duration.total_seconds())
                        if event.genres:
                            genre = event.genres[0]

                # 録画メタデータの登録
                now = datetime.now(timezone.utc).isoformat()
                rec_data = {
                    "file_name": path.name,
                    "file_size": file_size,
                    "file_hash": file_hash,
                    "service_id": service_id,
                    "original_network_id": original_network_id,
                    "transport_stream_id": transport_stream_id,
                    "service_name": service_name,
                    "provider_name": provider_name,
                    "broadcast_type": broadcast_type,
                    "program_name": program_name,
                    "genre": genre,
                    "program_description": description,
                    "program_extended": extended,
                    "broadcast_start": broadcast_start,
                    "broadcast_duration": broadcast_duration,
                    "recording_start": recording_start,
                    "recording_end": recording_end,
                    "video_codec": file_info.video_codec,
                    "audio_codec": file_info.audio_codec,
                    "width": file_info.resolution[0] if file_info.resolution else None,
                    "height": file_info.resolution[1] if file_info.resolution else None,
                    "scan_type": file_info.scan_type,
                    "fps": file_info.fps,
                    "bitrate": file_info.bitrate,
                    "total_frames": file_info.total_frames,
                    "updated_at": now,
                }
                if existing:
                    # カットし直し: 前のカットの記録は残し(is_active=0)、新しいカットを足す。
                    # 別の話(保存名称が違い、区間が重ならない)のカットは有効のまま。
                    # 特徴は有効なカットすべての区間で計算し直して置き換える
                    recording_id = existing["id"]
                    self.db.update_recording(conn, recording_id, rec_data)
                    replaced = self.db.deactivate_replaced_sessions(
                        conn, recording_id, output_name,
                        [(c.start_frame, c.end_frame) for c in file_info.cuts if not c.is_excluded],
                    )
                    logger.info(
                        "Already registered video (id=%d, registered %s). %s",
                        recording_id, existing["created_at"],
                        f"Replacing the earlier cut (replaced: {replaced})." if replaced
                        else "Adding it as the cut of another episode.",
                    )
                else:
                    rec_data["created_at"] = now
                    recording_id = self.db.insert_recording(conn, rec_data)

                # cut_sessions の作成
                if source_type is None:
                    source_type = _source_type(file_info.cuts, accepted)
                session_data = {
                    "recording_id": recording_id,
                    "finalized_at": now,
                    "processing_mode": processing_mode,
                    "source_type": source_type,
                    "app_version": __version__,
                    "is_active": 1,
                    "output_name": output_name,
                }
                track = file_info.audio_track  # 出力に使った音声 (§4.12。None = 最初の音声・記録なし)
                if track:
                    session_data.update({
                        "audio_index": track.get("index"), "audio_pid": track.get("pid"),
                        "audio_component_tag": track.get("component_tag"),
                        "audio_component_type": track.get("component_type"),
                        "audio_text": track.get("text") or None,
                    })
                session_id = self.db.insert_session(conn, session_data)

                # cut_ranges の保存(承認した案から直した量も)
                ranges_data = []
                for cut in file_info.cuts:
                    ranges_data.append({
                        "start_frame": cut.start_frame,
                        "end_frame": cut.end_frame,
                        "label": cut.label,
                        "is_excluded": cut.is_excluded,
                        **_proposal_fields(cut.start_frame, cut.end_frame, accepted),
                    })
                self.db.insert_cut_ranges(conn, session_id, ranges_data)

                # edit_events の保存
                if edit_events:
                    self.db.insert_edit_events(conn, session_id, edit_events)

                logger.info(
                    "Recorded the finalized cut: session_id=%d, recording_id=%d, cuts=%d, events=%d",
                    session_id,
                    recording_id,
                    len(ranges_data),
                    len(edit_events or []),
                )
                return session_id

        except Exception as e:
            logger.error("An exception occurred while recording the finalized cut: %s", e, exc_info=True)
            return None
