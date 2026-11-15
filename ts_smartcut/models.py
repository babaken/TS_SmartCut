"""Core dataclasses (design doc §1).

Each dataclass carries to_dict()/from_dict() for JSON round-tripping,
since dataclasses.asdict() alone does not know how to serialize Path
or datetime fields (used by ProjectManager, design doc §10.2).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _path_to_str(p: Optional[Path]) -> Optional[str]:
    return str(p) if p is not None else None


def _str_to_path(s: Optional[str]) -> Optional[Path]:
    return Path(s) if s is not None else None


@dataclass
class CutRange:
    """フレーム区間 [start_frame, end_frame)。FileCutInfo.cuts では「出力に
    残す区間」を表す（2026-10-06 に「削除する区間」から変更。利用者は本編を
    区間として指定する）。処理エンジン(SmartCutEngine.process)には
    removed_ranges() で「削除する区間」に変換して渡す。"""

    start_frame: int
    end_frame: int
    label: Optional[str] = None
    # 基準外フラグ(設計書 §31.8-1、自動カット計画 A2)。True の区間は自動カットの学習から除外する
    is_excluded: bool = False

    def to_dict(self) -> dict:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "label": self.label,
            "is_excluded": self.is_excluded,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CutRange":
        return cls(
            start_frame=data["start_frame"],
            end_frame=data["end_frame"],
            label=data.get("label"),
            is_excluded=data.get("is_excluded", False),
        )


def removed_ranges(total_frames: int, keep: List[CutRange]) -> List[CutRange]:
    """残す区間の補集合(削除する区間)を返す。範囲外は丸め、重なりはまとめる。"""
    removed: List[CutRange] = []
    cursor = 0
    for r in sorted(keep, key=lambda c: c.start_frame):
        start = max(0, min(r.start_frame, total_frames))
        end = max(0, min(r.end_frame, total_frames))
        if start > cursor:
            removed.append(CutRange(cursor, start))
        cursor = max(cursor, end)
    if cursor < total_frames:
        removed.append(CutRange(cursor, total_frames))
    return removed


@dataclass
class Bookmark:
    frame: int
    name: str
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "name": self.name,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bookmark":
        return cls(
            frame=data["frame"],
            name=data["name"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


@dataclass
class FileCutInfo:
    file_path: Path
    total_frames: int
    fps: float
    resolution: Tuple[int, int]
    video_codec: str
    audio_codec: str
    bitrate: int
    duration: float
    file_size: int
    # 出力に残す区間（CutRange のdocstring参照）
    cuts: List[CutRange] = field(default_factory=list)
    bookmarks: List[Bookmark] = field(default_factory=list)
    keyframes: Optional[List[int]] = None
    # 可変フレームレート(design doc §16)。Smart CutはSimple Copyに切り替わる
    is_vfr: bool = False
    # ffmpeg scan classification, stored with autocut history metadata.
    scan_type: str = ""
    # 音声(設計書 §4.12): 選べる音声の一覧と選んだ音声(broadcast.audio_tracks.AudioTrack の辞書)。
    # 選んだ音声が None なら最初の音声(番組情報の無い動画・音声が 1 つの録画・8.2 までのプロジェクト)
    audio_tracks: List[dict] = field(default_factory=list)
    audio_track: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "file_path": _path_to_str(self.file_path),
            "total_frames": self.total_frames,
            "fps": self.fps,
            "resolution": list(self.resolution),
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "bitrate": self.bitrate,
            "duration": self.duration,
            "file_size": self.file_size,
            "cuts": [c.to_dict() for c in self.cuts],
            "bookmarks": [b.to_dict() for b in self.bookmarks],
            "keyframes": self.keyframes,
            "is_vfr": self.is_vfr,
            "scan_type": self.scan_type,
            "audio_tracks": [dict(t) for t in self.audio_tracks],
            "audio_track": dict(self.audio_track) if self.audio_track else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileCutInfo":
        return cls(
            file_path=_str_to_path(data["file_path"]),
            total_frames=data["total_frames"],
            fps=data["fps"],
            resolution=tuple(data["resolution"]),
            video_codec=data["video_codec"],
            audio_codec=data["audio_codec"],
            bitrate=data["bitrate"],
            duration=data["duration"],
            file_size=data["file_size"],
            cuts=[CutRange.from_dict(c) for c in data.get("cuts", [])],
            bookmarks=[Bookmark.from_dict(b) for b in data.get("bookmarks", [])],
            keyframes=data.get("keyframes"),
            is_vfr=data.get("is_vfr", False),
            scan_type=data.get("scan_type", ""),
            audio_tracks=[dict(t) for t in data.get("audio_tracks", [])],
            audio_track=dict(data["audio_track"]) if data.get("audio_track") else None,
        )

    def selected_audio_index(self) -> int:
        """選んだ音声が一覧の何番目か(0 = 最初の音声)。"""
        return int(self.audio_track.get("index", 0)) if self.audio_track else 0


@dataclass
class BatchItem:
    order: int
    file_info: FileCutInfo
    status: str  # Idle / Processing / Done / Error / Skipped
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    output_path: Optional[Path] = None
    # "smart_cut" / "simple_copy" / "full_reencode" (design doc §6.1;
    # ffmpeg.smart_cut.MODE_* と同じ文字列値。3モードはアイテムごとに
    # 選択可能で、単純なon/offの bool では表現できないため文字列とする)
    processing_mode: str = "smart_cut"
    # 出力形式 "ts" / "avs" / "vpy" (設計書 §6.9、core.script_output.OUTPUT_*)。avs・vpy は処理モードを使わず
    # カット情報のスクリプトを書く
    output_format: str = "ts"
    # 自動カット用の番組情報(SI)と編集履歴(A2)
    program_info: Optional[Any] = None
    edit_events: List[dict] = field(default_factory=list)
    # 自動カットの案(CutProposal.to_dict()、A6)。記録の時に、承認した案と実際の区間の差を残す
    proposals: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "file_info": self.file_info.to_dict(),
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "output_path": _path_to_str(self.output_path),
            "processing_mode": self.processing_mode,
            "output_format": self.output_format,
            "edit_events": self.edit_events,
            "proposals": self.proposals,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchItem":
        return cls(
            order=data["order"],
            file_info=FileCutInfo.from_dict(data["file_info"]),
            status=data["status"],
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
            output_path=_str_to_path(data.get("output_path")),
            processing_mode=data.get("processing_mode", "smart_cut"),
            output_format=data.get("output_format", "ts"),
            edit_events=data.get("edit_events", []),
            proposals=data.get("proposals", []),
        )


@dataclass
class ProcessProgress:
    current_file: int
    total_files: int
    current_phase: str  # Head / Body / Tail / Audio / Concat
    progress_percent: float
    elapsed_time: float
    estimated_remaining: float
    # 段階の番号(0始まり)と数、いま処理している元のフレーム(無ければNone)
    current_step: int = 0
    total_steps: int = 0
    source_frame: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProcessProgress":
        return cls(**data)


# AppSettings のうち Path の項目(設定ファイルには文字で書く)
_SETTINGS_PATH_KEYS = ("temp_directory", "ffmpeg_path", "ffprobe_path", "mpv_path", "default_output_dir",
                       "avs_template", "vpy_template")


@dataclass
class AppSettings:
    # 全般
    # 自動保存・起動時に前回のプロジェクトを開く機能は要件から外した(2026-10-05)
    recent_files_count: int = 10

    # プレビュー
    audio_output_device: str = "auto"  # mpvの出力先の名前。"auto"=OSの既定
    playback_volume: int = 100
    show_waveform: bool = True  # プレビューの下の音声波形
    # 下のタブ(残す区間・ブックマーク・バッチリスト)を出すか。表示メニューで切り替える(2026-10-11)
    show_cut_list_tab: bool = True
    show_bookmark_tab: bool = True
    show_batch_tab: bool = True
    # バッチリストの列(順序・ファイル・モード・状態・エラー)の幅。空 = 初期値
    batch_column_widths: List[int] = field(default_factory=list)

    # 自動カット (設定の「自動カット」タブ、A9)
    autocut_proposals: bool = True  # 録画を開いた時に案を作る
    notify_logo_change: bool = True  # 局のロゴが変わった可能性を知らせる
    # 案の方式(A8): "rule" / "learned"。学習のモデルが無い・ルールより悪い局は学習でもルールで作る
    autocut_method: str = "learned"
    # 局ごとの方式: 局の鍵('<original_network_id>:<service_id>')→ "rule" / "learned"。無い局は既定の方式
    autocut_station_methods: Dict[str, str] = field(default_factory=dict)

    # エンコード
    # 再エンコードのコーデック・走査方式・画質(ビットレート)は元ソースに合わせて
    # 自動で決まるため設定項目にしない(Smart Cutが目的で、変換機能は目的外)。
    default_processing_mode: str = "smart_cut"  # smart_cut / simple_copy / full_reencode
    # 出力形式 ts / avs / vpy と、AVS・VPY のテンプレートのファイル(None = 同梱の既定。設計書 §6.9)
    output_format: str = "ts"
    avs_template: Optional[Path] = None
    vpy_template: Optional[Path] = None
    cpu_threads: int = 0  # 0=auto
    temp_directory: Optional[Path] = None

    # UI
    theme: str = "light"  # light / dark
    language: str = "jp"

    # ログ
    log_enabled: bool = True
    log_level: str = "INFO"  # ERROR / WARNING / INFO / DEBUG / TRACE

    # 詳細
    ffmpeg_path: Optional[Path] = None
    ffprobe_path: Optional[Path] = None
    mpv_path: Optional[Path] = None

    # 出力
    default_output_dir: Optional[Path] = None
    output_filename_pattern: str = "{name}_cut{ext}"

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in _SETTINGS_PATH_KEYS:
            data[key] = _path_to_str(getattr(self, key))
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        # 以前の版にあった項目(crf_value, preset, gpu_enabled等)を含む設定ファイル・
        # プロジェクトファイルも読めるよう、知らない項目は無視する
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        for key in _SETTINGS_PATH_KEYS:
            if key in data:
                data[key] = _str_to_path(data[key])
        return cls(**data)


@dataclass
class Project:
    version: str
    file_info: FileCutInfo
    settings: AppSettings
    created_at: datetime
    modified_at: datetime
    edit_events: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "file_info": self.file_info.to_dict(),
            "settings": self.settings.to_dict(),
            "created_at": self.created_at.isoformat(),
            "modified_at": self.modified_at.isoformat(),
            "edit_events": self.edit_events,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            version=data["version"],
            file_info=FileCutInfo.from_dict(data["file_info"]),
            settings=AppSettings.from_dict(data["settings"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            modified_at=datetime.fromisoformat(data["modified_at"]),
            edit_events=data.get("edit_events", []),
        )


PROPOSAL_PENDING = "pending"
PROPOSAL_ACCEPTED = "accepted"
PROPOSAL_REJECTED = "rejected"


@dataclass(eq=False)
class CutProposal:
    """自動カットの「残す区間の案」[start_frame, end_frame) (設計書 §31.6、計画 A6)。
    承認すると残す区間になり、棄却すると捨てる。eq=False: 同じ範囲の案も別のものとして扱う。"""

    start_frame: int
    end_frame: int
    confidence: float
    reason: str
    status: str = PROPOSAL_PENDING

    def to_dict(self) -> dict:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "confidence": self.confidence,
            "reason": self.reason,
            "status": self.status,
        }


@dataclass
class OperationHistory:
    """Undo/Redo用の操作履歴 (design doc §7). In-memory only; `data` holds
    whatever the recording call site passed (e.g. CutRange instances) and is
    not intended for JSON persistence as-is."""

    operation_type: str  # add_cut / remove_cut / edit_cut / set_start / set_end
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "operation_type": self.operation_type,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OperationHistory":
        return cls(
            operation_type=data["operation_type"],
            data=data["data"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )
