"""メインウィンドウ (design doc §4、Qt6/PySide6版)。

Phase 7のスコープは「GUIの骨格」: メニュー構成・レイアウト・テーマ適用、
およびmpv render APIによる実際に動くプレビュー再生。カット編集・バッチ・
ブックマーク等の機能はPhase 8で実装するため、対応するメニュー項目は
存在するが無効化(disabled)しておく — 動かない機能を動くように見せない。

mpvはソフト依存 (design doc §12.2, CLAUDE.md): 接続できない場合は警告を
表示した上でプレビュー制限モード（静止プレースホルダー）へフォール
バックし、ウィンドウ自体は問題なく使用できる。
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QShortcut, QSurfaceFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ts_smartcut.broadcast.audio_tracks import AudioTrack, audio_tracks, same_track
from ts_smartcut.broadcast.program_info import ProgramInfo, read_program_info
from ts_smartcut.autocut.auto_tuning import AutoTuner, stations_of_recordings
from ts_smartcut.autocut.feature_worker import FeatureWorker
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.lgd_import import check_imported_logo, sync_logo_folder
from ts_smartcut.autocut.logo_change import detect_logo_change
from ts_smartcut.autocut.proposals import build_file_proposals
from ts_smartcut.autocut.recorder import AutoCutRecorder, compute_file_hash
from ts_smartcut.autocut.thresholds import station_key
from ts_smartcut.core.batch_manager import STATUS_DONE, BatchManager
from ts_smartcut.core.bookmark_manager import BookmarkManager
from ts_smartcut.core.cut_manager import CutManager
from ts_smartcut.core.project_manager import CURRENT_PROJECT_VERSION, ProjectManager
from ts_smartcut.core.recent_files_manager import RecentFilesManager
from ts_smartcut.core.settings_manager import SettingsManager
from ts_smartcut.core.shortcut_presets import PRESET_COUNT, ShortcutPresetManager, display_key
from ts_smartcut.core.waveform import AudioEnvelope, generate_envelope
from ts_smartcut.errors import TSSmartCutError, ValidationError
from ts_smartcut.ffmpeg.smart_cut import resolve_processing_mode
from ts_smartcut.ffmpeg.wrapper import (
    SCAN_INTERLACED,
    SCAN_MIXED,
    SCAN_PROGRESSIVE,
    SCAN_SOFT_TELECINE,
    FFmpegWrapper,
)
from ts_smartcut.gui.about_dialog import show_about_dialog
from ts_smartcut.gui.audio_select_dialog import AudioSelectDialog
from ts_smartcut.gui.batch_list_view import BatchListView
from ts_smartcut.gui import output_name_dialog
from ts_smartcut.gui.preview_widget import PreviewWidget
from ts_smartcut.gui.seek_slider import SeekSlider
from ts_smartcut.gui.settings_dialog import SettingsDialog
from ts_smartcut.gui.shortcuts_dialog import ShortcutsDialog
from ts_smartcut.gui.system_info_dialog import SystemInfoDialog
from ts_smartcut.gui.theme import SYSTEM_THEME, ThemeManager
from ts_smartcut.gui.timeline import PROPOSAL_COLOR, TimelineWidget
from ts_smartcut.gui.waveform_view import WaveformView
from ts_smartcut.i18n.language_manager import DEFAULT_LANGUAGE, LanguageManager
from ts_smartcut.i18n.language_manager import set_current as set_current_language
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.logging_setup import configure_logging
from ts_smartcut.models import AppSettings, FileCutInfo, Project
from ts_smartcut.mpv import is_mpv_available
from ts_smartcut.time_utils import format_time, frame_to_display

logger = logging.getLogger(__name__)

# ステータスバーの知らせを出しておく時間(区間を S / E で直した時など)
STATUS_MESSAGE_MS = 5000
THRESHOLDS_MESSAGE_MS = 15000  # しきい値の自動補正の知らせ(バッチの後で、見ていないことが多い)

# メインウィンドウへドロップして開けるファイル(TS録画とプロジェクトファイル)
DROPPABLE_SUFFIXES = {".ts", ".m2ts", ".mts", ".json"}

# ステータスバーの空き容量の更新間隔(処理中に減っていくのが分かる程度)
DISK_FREE_REFRESH_MS = 10_000

# ファイル情報の行に出す走査方式 (FFmpegWrapper.get_scan_type)
SCAN_LABELS = {
    SCAN_PROGRESSIVE: "main.scan_progressive",
    SCAN_INTERLACED: "main.scan_interlaced",
    SCAN_SOFT_TELECINE: "main.scan_soft_telecine",
    SCAN_MIXED: "main.scan_mixed",
}

PLAYBACK_SPEEDS = ["0.25", "0.5", "1.0", "1.5", "2.0"]
POSITION_POLL_INTERVAL_MS = 200

# mpv render APIはOpenGLコンテキストを要求する (design doc §2)。
_SURFACE_FORMAT = QSurfaceFormat()
_SURFACE_FORMAT.setVersion(3, 3)
_SURFACE_FORMAT.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(_SURFACE_FORMAT)


class _EnvelopeBridge(QObject):
    """音声波形を作ったスレッドから、結果をGUIのスレッドへ渡す(design doc §29)。"""

    ready = Signal(int, object, float)


class _ProgramInfoBridge(QObject):
    """番組情報(局名・番組名)を読んだスレッドから、結果をGUIのスレッドへ渡す。
    registered は自動カットの記録 DB に登録済みの動画か(未登録なら None)。"""

    ready = Signal(int, object)
    registered = Signal(int, object)
    # 自動カットの案(A6): (request, CutProposal の一覧 or None, 状態の文)
    proposals = Signal(int, object, str)
    # 案の準備の進み具合: (request, 読んだコマの数)
    progress = Signal(int, int)
    # 取り込んだ局ロゴが録画のロゴと合わない (lgd_import.StaleLogo)
    stale_logo = Signal(int, object)
    # 局のロゴが変わった可能性 (logo_change.LogoChange)
    logo_change = Signal(int, object)


class _LoadingDialog(QDialog):
    """録画の読み込み中のポップアップ (設計書 §4.11、2026-10-09)。段階と進み具合を出す。
    ほかの操作はメイン画面の側で止める(モーダルにするとメイン画面の×も効かなくなり、
    「読み込み中も閉じられる」と合わないため)。"""

    skip_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("main.loading_title"))
        self.setModal(False)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        self.label = QLabel("")
        layout.addWidget(self.label)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.skip_button = QPushButton(tr("main.loading_skip"))
        self.skip_button.setToolTip(tr("main.loading_skip_tip"))
        self.skip_button.clicked.connect(self.skip_requested.emit)
        layout.addWidget(self.skip_button)
        self.stage = ""

    def set_stage(self, stage: str, text: str, percent: Optional[float] = None) -> None:
        self.stage = stage
        self.label.setText(text)
        if percent is None:
            self.progress.setRange(0, 0)  # 動いていることだけ示す
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(max(0.0, min(percent, 100.0))))
        self.skip_button.setVisible(stage == "proposals")

    def reject(self) -> None:
        # Esc・×: 案の準備中は「案を待たずに編集する」と同じ。ほかの段階では閉じない
        if self.stage == "proposals":
            self.skip_requested.emit()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.reject()


class _ProposalStop:
    """案の準備をやめる合図(compute_features は is_set() だけを使う)。別の録画を開いた・
    アプリを閉じる・バッチ処理やエンコードを始めた(変換と同時に動かさない)時に立つ。"""

    def __init__(self, window: "MainWindow"):
        self.event = threading.Event()
        self._window = window

    def set(self) -> None:
        self.event.set()

    def is_set(self) -> bool:
        w = self._window
        return self.event.is_set() or w.batch_manager.is_running or w.is_encoding()


class _AutoTuneBridge(QObject):
    """しきい値の自動補正のスレッドから、採用した結果を GUI のスレッドへ渡す。"""

    adopted = Signal(object)  # List[AutoTuneOutcome]


class _EncodeBridge(QObject):
    """エンコード(エンコード開始ボタン)のスレッドから進捗をGUIのスレッドへ渡す。"""

    progress = Signal(object)


# 音声の選択で聞く位置の初め: 番組の放送開始から何秒後か(設計書 §4.12)
LISTEN_AFTER_PROGRAMME_START = 10.0


class _AudioDialogPreview:
    """音声の選択の画面が使うプレビューの操作(設計書 §4.12)。音を鳴らすため、再生の時は消音を外す
    (閉じた後に MainWindow が元に戻す)。"""

    def __init__(self, window: "MainWindow") -> None:
        self._w = window

    def seek_frame(self, frame: int) -> None:
        if self._w.preview_widget.is_connected:
            try:
                self._w.preview_widget.seek_frame(frame)
            except TSSmartCutError as e:
                logger.warning("Could not seek while choosing the audio: %s", e.log_message)

    def play(self) -> None:
        self._w.preview_widget.set_mute(False)
        self._w.preview_widget.command(["set", "pause", "no"])

    def pause(self) -> None:
        self._w.preview_widget.command(["set", "pause", "yes"])

    def set_audio_pid(self, pid) -> None:
        self._w.preview_widget.set_audio_pid(pid)

    def current_frame(self):
        return self._w.preview_widget.get_current_frame()


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        recent_files: Optional[RecentFilesManager] = None,
        shortcut_presets: Optional[ShortcutPresetManager] = None,
    ):
        super().__init__()
        self.settings = settings or AppSettings()
        self.recent_files = recent_files or RecentFilesManager(max_count=self.settings.recent_files_count)
        self.shortcut_presets = shortcut_presets or ShortcutPresetManager()
        try:
            self.lang = LanguageManager(self.settings.language)
        except TSSmartCutError:
            # 言語ファイル欠如(E402)時はデフォルト言語にフォールバックし、
            # 起動不能にしない (design doc §12系のソフト依存方針と同じ考え方)。
            logger.warning("Could not load the language file, falling back to the default language: %s", self.settings.language)
            self.lang = LanguageManager(DEFAULT_LANGUAGE)
        set_current_language(self.lang)
        self.ffmpeg = FFmpegWrapper(self.settings)
        self.file_info: Optional[FileCutInfo] = None
        self.cut_manager: Optional[CutManager] = None
        self.bookmark_manager = BookmarkManager()
        # 自動カットの記録 DB(A2)。開けない(版がアプリより新しい等)時はエラーを出し、
        # 記録と特徴の計算をしない(カット処理は止めない)
        self.history_db = self._open_history_db()
        # バッチで記録した録画の特徴(自動カット A3)は、バッチが全部終わった後に裏で計算する
        self.feature_worker = FeatureWorker(db=self.history_db)
        # しきい値の自動補正(設計書 §31.9、2026-10-15)。バッチが終わった後に裏で補正し、条件に合えば自動で採用する
        self._auto_tune_bridge = _AutoTuneBridge()
        self._auto_tune_bridge.adopted.connect(self._on_thresholds_adopted)
        self.auto_tuner: Optional[AutoTuner] = None
        if self.history_db is not None:
            self.auto_tuner = AutoTuner(self.history_db, on_finished=self._auto_tune_bridge.adopted.emit)
        self.batch_manager = BatchManager(
            self.settings,
            autocut_recorder=AutoCutRecorder(self.history_db) if self.history_db is not None else None,
            record_autocut=self.history_db is not None,
            feature_worker=self.feature_worker,
        )
        # エンコード開始ボタンの処理。バッチリストとは別の1件だけの処理役を使う。
        # 実行中は画面の操作を受け付けない(中止ボタンだけ押せる)。
        self.encode_manager: Optional[BatchManager] = None
        self._encode_bridge = _EncodeBridge()
        self._encode_bridge.progress.connect(self._on_encode_progress)
        self._encode_last_frame: Optional[int] = None
        self._keyframes: List[int] = []
        self._envelope: Optional[AudioEnvelope] = None
        # 音声波形は裏のスレッドで作る(48分の録画で約2秒)。別のファイルを開いた後に
        # 届いた古い結果は捨てる
        self._envelope_request = 0
        self._envelope_bridge = _EnvelopeBridge()
        self._envelope_bridge.ready.connect(self._on_envelope_ready)
        # 番組情報(TS の SDT/EIT/TOT。自動カット A1)も裏のスレッドで読む(1秒未満)
        self.program_info: Optional[ProgramInfo] = None
        self._file_info_text = ""
        self._program_info_request = 0
        self._program_info_bridge = _ProgramInfoBridge()
        self._program_info_bridge.ready.connect(self._on_program_info_ready)
        self._program_info_bridge.registered.connect(self._on_registration_ready)
        self._program_info_bridge.proposals.connect(self._on_proposals_ready)
        self._program_info_bridge.progress.connect(self._on_proposal_progress)
        self._program_info_bridge.stale_logo.connect(self._on_stale_logo)
        self._program_info_bridge.logo_change.connect(self._on_logo_change)
        self._pending_stale_logo = None
        self._pending_logo_change = None
        self._proposal_stop: Optional[_ProposalStop] = None
        # 録画の読み込み中(ポップアップを出し、ほかの操作を止める。§4.11)
        self._loading = False
        self._loading_request = 0
        self._loading_dialog: Optional[_LoadingDialog] = None
        self._closing = False
        # 開いた動画の自動カットの記録 DB での登録(登録日時等。未登録なら None)
        self.registration: Optional[dict] = None
        # 音は初期状態で消す(利用者の要件 2026-10-06)。フレーム送りの間も鳴らさない
        self._muted = True
        self._preset_audio_track: Optional[dict] = None  # プロジェクトに保存した音声 (§4.12)
        self._muted_for_stepping = False
        self._pending_start: Optional[int] = None
        self._pending_end: Optional[int] = None
        self._selected_cut_index: Optional[int] = None
        self._seek_slider_dragging = False
        # 現在編集中のプロジェクトファイル (design doc §10)。動画を直接開いた
        # 直後はNone（未保存）で、「保存」は「名前を付けて保存」として動く。
        self._project_path: Optional[Path] = None
        self._project_created_at: Optional[datetime] = None

        self.setWindowTitle("TS_SmartCut")
        self.resize(1000, 720)

        from PySide6.QtWidgets import QApplication

        self.theme = ThemeManager.apply_theme(QApplication.instance(), self.settings.theme)
        # 「OS に従う」の時は、OS のライト/ダークの切り替えに合わせて配色を変える(§24)
        hints = QApplication.instance().styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._on_os_color_scheme_changed)

        self._build_menu()
        self._build_layout()
        self._build_shortcuts()
        self.preview_widget.connection_ready.connect(self._on_mpv_connection_ready)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POSITION_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_position)
        self._poll_timer.start()

    # ---- 構築 -----------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = self.menuBar()
        t = self.lang.get

        file_menu = menubar.addMenu(t("menu.file"))
        # キーは _build_shortcuts() の QShortcut で割り当て、メニューには表示だけ(_refresh_key_labels)。
        # キープリセットで変えられるため(設計書 §4.5.1)
        self.open_action = QAction(t("menu.open"), self)
        self.open_action.triggered.connect(self._on_open)
        file_menu.addAction(self.open_action)
        # 表示のたびに作り直す（存在しなくなったファイルを除くため、design doc §10.4）
        self.recent_menu = file_menu.addMenu(t("menu.recent_files"))
        self.recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        self.save_project_action = QAction(t("menu.save_project"), self)
        self.save_project_action.triggered.connect(self._on_save_project)
        file_menu.addAction(self.save_project_action)
        open_project_action = QAction(t("menu.open_project"), self)
        open_project_action.triggered.connect(self._on_open_project)
        file_menu.addAction(open_project_action)
        self.save_project_as_action = QAction(t("menu.save_project_as"), self)
        self.save_project_as_action.triggered.connect(self._on_save_project_as)
        file_menu.addAction(self.save_project_as_action)
        file_menu.addSeparator()
        settings_action = QAction(t("menu.settings"), self)
        settings_action.triggered.connect(self._on_open_settings)
        file_menu.addAction(settings_action)
        self.exit_action = QAction(t("menu.exit"), self)
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        edit_menu = menubar.addMenu(t("menu.edit"))
        self.undo_action = QAction(t("menu.undo"), self)
        self.undo_action.triggered.connect(self._on_undo)
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction(t("menu.redo"), self)
        self.redo_action.triggered.connect(self._on_redo)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        self.set_start_action = QAction(t("menu.set_start"), self)
        self.set_start_action.triggered.connect(self._on_set_start)
        edit_menu.addAction(self.set_start_action)
        self.set_end_action = QAction(t("menu.set_end"), self)
        self.set_end_action.triggered.connect(self._on_set_end)
        edit_menu.addAction(self.set_end_action)
        self.add_cut_action = QAction(t("menu.add_cut"), self)
        self.add_cut_action.triggered.connect(self._on_add_cut)
        edit_menu.addAction(self.add_cut_action)
        self.delete_cut_action = QAction(t("menu.delete_cut"), self)
        self.delete_cut_action.triggered.connect(self._on_delete_cut)
        edit_menu.addAction(self.delete_cut_action)
        self.edit_cut_action = QAction(t("menu.edit_cut"), self)
        self.edit_cut_action.triggered.connect(self._on_edit_cut)
        edit_menu.addAction(self.edit_cut_action)
        edit_menu.addSeparator()
        self.add_bookmark_action = QAction(t("menu.add_bookmark"), self)
        self.add_bookmark_action.triggered.connect(self._on_add_bookmark)
        edit_menu.addAction(self.add_bookmark_action)
        self.remove_bookmark_action = QAction(t("menu.remove_bookmark"), self)
        self.remove_bookmark_action.triggered.connect(self._on_remove_bookmark)
        edit_menu.addAction(self.remove_bookmark_action)
        edit_menu.addSeparator()
        self.clear_all_action = QAction(t("menu.clear_all"), self)
        self.clear_all_action.triggered.connect(self._on_clear_all)
        edit_menu.addAction(self.clear_all_action)

        view_menu = menubar.addMenu(t("menu.view"))
        theme_menu = QMenu(t("menu.theme"), self)
        light_action = QAction(t("menu.theme_light"), self)
        light_action.triggered.connect(lambda: self._set_theme("light"))
        theme_menu.addAction(light_action)
        dark_action = QAction(t("menu.theme_dark"), self)
        dark_action.triggered.connect(lambda: self._set_theme("dark"))
        theme_menu.addAction(dark_action)
        system_action = QAction(t("menu.theme_system"), self)
        system_action.triggered.connect(lambda: self._set_theme(SYSTEM_THEME))
        theme_menu.addAction(system_action)
        view_menu.addMenu(theme_menu)
        view_menu.addSeparator()
        self.waveform_action = QAction(t("menu.waveform"), self)
        self.waveform_action.setCheckable(True)
        self.waveform_action.setChecked(self.settings.show_waveform)
        self.waveform_action.toggled.connect(self._on_toggle_waveform)
        view_menu.addAction(self.waveform_action)
        # 下のタブを出す・隠す(2026-10-11。設定に覚える)
        view_menu.addSeparator()
        self.cut_list_tab_action = QAction(t("menu.show_cut_list_tab"), self)
        self.bookmark_tab_action = QAction(t("menu.show_bookmark_tab"), self)
        self.batch_tab_action = QAction(t("menu.show_batch_tab"), self)
        for action, key in ((self.cut_list_tab_action, "show_cut_list_tab"),
                            (self.bookmark_tab_action, "show_bookmark_tab"),
                            (self.batch_tab_action, "show_batch_tab")):
            action.setCheckable(True)
            action.setChecked(getattr(self.settings, key))
            action.toggled.connect(lambda checked, k=key: self._on_toggle_tab(k, checked))
            view_menu.addAction(action)
        # キープリセットの切り替え(設計書 §4.5.1)。名前が変わるので表示のたびに作り直す
        view_menu.addSeparator()
        self.shortcut_preset_menu = view_menu.addMenu(t("menu.shortcut_preset"))
        self.shortcut_preset_menu.aboutToShow.connect(self._rebuild_shortcut_preset_menu)
        self._rebuild_shortcut_preset_menu()

        tools_menu = menubar.addMenu(t("menu.tools"))
        add_to_batch_action = QAction(t("menu.add_to_batch"), self)
        add_to_batch_action.triggered.connect(self._on_add_to_batch)
        tools_menu.addAction(add_to_batch_action)
        view_batch_action = QAction(t("menu.view_batch"), self)
        view_batch_action.triggered.connect(self._on_show_batch_tab)
        tools_menu.addAction(view_batch_action)
        start_batch_action = QAction(t("menu.start_batch"), self)
        start_batch_action.triggered.connect(self._on_start_batch)
        tools_menu.addAction(start_batch_action)
        stop_batch_action = QAction(t("menu.stop_batch"), self)
        stop_batch_action.triggered.connect(self._on_stop_batch)
        tools_menu.addAction(stop_batch_action)
        tools_menu.addSeparator()
        logo_folder_action = QAction(t("menu.open_logo_folder"), self)
        logo_folder_action.triggered.connect(self._on_open_logo_folder)
        tools_menu.addAction(logo_folder_action)

        help_menu = menubar.addMenu(t("menu.help"))
        self.shortcuts_action = QAction(t("menu.shortcuts"), self)
        self.shortcuts_action.triggered.connect(self._on_show_shortcuts)
        help_menu.addAction(self.shortcuts_action)
        system_info_action = QAction(t("menu.system_info"), self)
        system_info_action.triggered.connect(self._on_show_system_info)
        help_menu.addAction(system_info_action)
        about_action = QAction(t("menu.version_info"), self)
        about_action.triggered.connect(self._on_show_about)
        help_menu.addAction(about_action)

    def _build_layout(self) -> None:
        t = self.lang.get
        central = QWidget(self)
        self.setCentralWidget(central)
        self.setAcceptDrops(True)  # TS/プロジェクトファイルのドロップで開く (design doc §4.6)
        layout = QVBoxLayout(central)

        self.file_info_label = QLabel(t("labels.file_not_loaded"))
        # 局名・番組名・ファイル名で長くなるので、ウィンドウの幅を押し広げずに右端を切る
        # (全文はツールチップ)
        self.file_info_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.file_info_label.setTextFormat(Qt.TextFormat.PlainText)  # 番組名の「<…>」を HTML にしない
        layout.addWidget(self.file_info_label)

        # プレビュー領域 (design doc §4.1)。mpv接続に成功すればここへ
        # render APIで描画する。失敗時はプレースホルダーページに切替える。
        self.preview_stack = QStackedWidget()
        self.preview_widget = PreviewWidget()
        self.preview_stack.addWidget(self.preview_widget)
        self.preview_placeholder = QLabel(self.lang.get("messages.mpv_disconnected"))
        self.preview_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_placeholder.setStyleSheet("background-color: black; color: white;")
        self.preview_stack.addWidget(self.preview_placeholder)
        # プレビューに最小の高さは設けない。ウィンドウを低くすると、まずプレビューが縮む
        # (360 に決めていた時は画面の最小の高さが 804 になり、1080p の画面を 150% で使うと
        # 下の一覧がはみ出した。利用者の決定 2026-10-09)
        layout.addWidget(self.preview_stack, stretch=1)

        controls_layout = QHBoxLayout()
        self.back5s_button = QPushButton("⏮5s")
        self.back5s_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.back5s_button.clicked.connect(self._on_back_5s)
        controls_layout.addWidget(self.back5s_button)

        self.frame_back_button = QPushButton("⏮1f")
        self.frame_back_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.frame_back_button.clicked.connect(self._on_frame_back)
        controls_layout.addWidget(self.frame_back_button)

        self.play_pause_button = QPushButton("▶/⏸")
        self.play_pause_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_pause_button.clicked.connect(self._on_toggle_pause)
        controls_layout.addWidget(self.play_pause_button)

        self.frame_forward_button = QPushButton("⏭1f")
        self.frame_forward_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.frame_forward_button.clicked.connect(self._on_frame_forward)
        controls_layout.addWidget(self.frame_forward_button)

        self.forward5s_button = QPushButton("⏭5s")
        self.forward5s_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.forward5s_button.clicked.connect(self._on_forward_5s)
        controls_layout.addWidget(self.forward5s_button)

        # 選んだ行(案の行も)の開始・終了へ跳ぶ(設計書 §4.3、2026-10-14)
        self.jump_start_button = QPushButton(t("buttons.jump_start"))
        self.jump_start_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.jump_start_button.setToolTip(t("buttons.jump_start_tip"))
        self.jump_start_button.clicked.connect(self._on_jump_to_selected_start)
        controls_layout.addWidget(self.jump_start_button)
        self.jump_end_button = QPushButton(t("buttons.jump_end"))
        self.jump_end_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.jump_end_button.setToolTip(t("buttons.jump_end_tip"))
        self.jump_end_button.clicked.connect(self._on_jump_to_selected_end)
        controls_layout.addWidget(self.jump_end_button)

        controls_layout.addWidget(QLabel(t("labels.speed")))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(PLAYBACK_SPEEDS)
        self.speed_combo.setCurrentText("1.0")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        controls_layout.addWidget(self.speed_combo)

        # 音声 (設計書 §4.12)。消音の左隣(右隣はエンコード開始・バッチ追加、§4.10)。録画に音声が
        # 2 つ以上ある時だけ選べる。変えるとプレビューと出力の音声が変わる
        self.audio_label = QLabel(t("audio.combo_label"))
        controls_layout.addWidget(self.audio_label)
        self.audio_combo = QComboBox()
        self.audio_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.audio_combo.setToolTip(t("audio.combo_tip"))
        self.audio_combo.setEnabled(False)
        self.audio_combo.currentIndexChanged.connect(self._on_audio_combo_changed)
        controls_layout.addWidget(self.audio_combo)

        self.mute_button = QPushButton()
        self.mute_button.setCheckable(True)
        self.mute_button.setChecked(True)
        self.mute_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mute_button.toggled.connect(self._on_mute_toggled)
        self._update_mute_button()
        controls_layout.addWidget(self.mute_button)

        # 今の残す区間ですぐエンコードする(バッチリストに入れない)/バッチリストに追加する
        self.start_encode_button = QPushButton(t("buttons.start_encode"))
        self.start_encode_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.start_encode_button.clicked.connect(self._on_start_encode)
        controls_layout.addWidget(self.start_encode_button)
        self.add_to_batch_button = QPushButton(t("buttons.add_to_batch"))
        self.add_to_batch_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.add_to_batch_button.clicked.connect(self._on_add_to_batch)
        controls_layout.addWidget(self.add_to_batch_button)
        controls_layout.addStretch(1)
        layout.addLayout(controls_layout)

        self.time_label = QLabel("00:00:00.000 (Frame: 0 / 0)")
        layout.addWidget(self.time_label)

        self.seek_slider = SeekSlider()
        self.seek_slider.setMinimum(0)
        self.seek_slider.setMaximum(1)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seek_slider_dragging", True))
        self.seek_slider.sliderReleased.connect(self._on_seek_slider_released)
        layout.addWidget(self.seek_slider)

        # タイムライン (design doc §4.7)
        self.timeline = TimelineWidget()
        self.timeline.set_theme(self.theme)
        self.timeline.frame_clicked.connect(self._on_timeline_clicked)
        layout.addWidget(self.timeline)

        # 音声波形 (design doc §21)。今の位置の前後を表示する。設定で表示/非表示
        self.waveform_view = WaveformView()
        self.waveform_view.set_theme(self.theme)
        self.waveform_view.frame_clicked.connect(self._on_timeline_clicked)
        self.waveform_view.setVisible(self.settings.show_waveform)
        layout.addWidget(self.waveform_view)

        # カット編集操作 (design doc §4.4)
        cut_edit_layout = QHBoxLayout()
        self.set_start_button = QPushButton(t("buttons.set_start"))
        self.set_start_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_start_button.setToolTip(t("buttons.set_start_tip"))
        self.set_start_button.clicked.connect(self._on_set_start)
        cut_edit_layout.addWidget(self.set_start_button)

        self.set_end_button = QPushButton(t("buttons.set_end"))
        self.set_end_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_end_button.setToolTip(t("buttons.set_end_tip"))
        self.set_end_button.clicked.connect(self._on_set_end)
        cut_edit_layout.addWidget(self.set_end_button)

        self.add_cut_button = QPushButton(t("buttons.add_cut"))
        self.add_cut_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.add_cut_button.clicked.connect(self._on_add_cut)
        cut_edit_layout.addWidget(self.add_cut_button)

        self.edit_cut_button = QPushButton(t("buttons.edit_cut"))
        self.edit_cut_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.edit_cut_button.clicked.connect(self._on_edit_cut)
        cut_edit_layout.addWidget(self.edit_cut_button)

        self.delete_cut_button = QPushButton(t("buttons.delete_cut"))
        self.delete_cut_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.delete_cut_button.clicked.connect(self._on_delete_cut)
        cut_edit_layout.addWidget(self.delete_cut_button)

        self.pending_label = QLabel("Start: - / End: -")
        cut_edit_layout.addWidget(self.pending_label)
        cut_edit_layout.addStretch(1)
        layout.addLayout(cut_edit_layout)

        # 残す区間・ブックマーク・バッチリストのタブ (design doc §4.1)。前は残す区間と
        # ブックマークが横に並び、バッチリストは別の窓だった(2026-10-11 利用者の要望でタブに)
        self.lists_tabs = QTabWidget()
        cut_list_page = QWidget()
        cut_list_layout = QVBoxLayout(cut_list_page)
        cut_list_layout.setContentsMargins(4, 4, 4, 4)
        title_layout = QHBoxLayout()
        self.cut_list_title = QLabel()
        title_layout.addWidget(self.cut_list_title)
        # 自動カットの案(青の行)の状態と、承認・棄却 (設計書 §31.6)
        self.proposal_status_label = QLabel("")
        title_layout.addWidget(self.proposal_status_label)
        title_layout.addStretch(1)
        self.accept_proposal_button = QPushButton(tr("proposals.accept"))
        self.accept_proposal_button.setToolTip(tr("proposals.accept_tip"))
        self.accept_proposal_button.clicked.connect(lambda: self._on_accept_proposal())
        self.reject_proposal_button = QPushButton(tr("proposals.reject"))
        self.reject_proposal_button.setToolTip(tr("proposals.reject_tip"))
        self.reject_proposal_button.clicked.connect(lambda: self._on_reject_proposal())
        self.accept_all_proposals_button = QPushButton(tr("proposals.accept_all"))
        self.accept_all_proposals_button.setToolTip(tr("proposals.accept_all_tip"))
        self.accept_all_proposals_button.clicked.connect(self._on_accept_all_proposals)
        for button in (self.accept_proposal_button, self.reject_proposal_button, self.accept_all_proposals_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setEnabled(False)
            title_layout.addWidget(button)
        cut_list_layout.addLayout(title_layout)
        self._update_cut_list_title()
        self.cut_list = QTreeWidget()
        self.cut_list.setHeaderLabels(
            [t("labels.column_start"), t("labels.column_end"), t("labels.column_label")]
        )
        self.cut_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.cut_list.itemSelectionChanged.connect(self._on_cut_selection_changed)
        # クリックした区間(案も)の開始フレームへ移動する(2026-10-09 利用者の決定)
        self.cut_list.itemClicked.connect(self._on_cut_item_clicked)
        # ダブルクリックで区間編集(残す区間の行だけ。未判断の案の行は何もしない、2026-10-11)
        self.cut_list.itemDoubleClicked.connect(self._on_cut_item_double_clicked)
        self.cut_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cut_list.customContextMenuRequested.connect(self._on_cut_context_menu)
        # 何も無い所のクリックで選択を外す(S / E を新しい区間用に戻す。設計書 §4.3、2026-10-14)
        self.cut_list.viewport().installEventFilter(self)
        self.timeline.cut_context_requested.connect(self._on_toggle_excluded_at_frame)
        cut_list_layout.addWidget(self.cut_list)
        self.cut_list_page = cut_list_page

        self.bookmark_list = QTreeWidget()
        self.bookmark_list.setHeaderLabels([t("labels.column_frame"), t("labels.column_name")])
        self.bookmark_list.itemDoubleClicked.connect(self._on_bookmark_double_clicked)

        # バッチリスト。タブの名前に件数(実行中は終わった数/全部)を出す。追加してもタブは
        # 切り替えない(編集を続けられるように)。列の幅は設定に覚える
        self.batch_list_view = BatchListView(self.batch_manager)
        self.batch_list_view.set_column_widths(self.settings.batch_column_widths)
        self.batch_list_view.column_widths_changed.connect(self._on_batch_column_widths_changed)
        self.batch_list_view.title_changed.connect(self._on_batch_title_changed)
        self.batch_list_view.batch_finished.connect(self._on_batch_finished)

        self.lists_tabs.addTab(cut_list_page, t("labels.cut_list_title"))
        self.lists_tabs.addTab(self.bookmark_list, t("labels.bookmark_list_title"))
        self.lists_tabs.addTab(self.batch_list_view, self.batch_list_view.title())
        layout.addWidget(self.lists_tabs, stretch=1)
        self._apply_tab_visibility()

        self._set_playback_controls_enabled(False)
        self._set_cut_edit_enabled(False)

        self.status_bar = self.statusBar()
        # エンコード中だけ出す: 進み具合と中止ボタン(左から順に右端の空き容量の前)
        self.encode_label = QLabel("")
        self.encode_progress_bar = QProgressBar()
        self.encode_progress_bar.setRange(0, 100)
        self.encode_progress_bar.setFixedWidth(240)
        self.cancel_encode_button = QPushButton(t("buttons.cancel_encode"))
        self.cancel_encode_button.clicked.connect(self._on_cancel_encode)
        for widget in (self.encode_label, self.encode_progress_bar, self.cancel_encode_button):
            self.status_bar.addPermanentWidget(widget)
            widget.hide()
        # 出力先の空き容量を右端に常に出す (design doc §4.1)
        self.disk_free_label = QLabel("")
        self.status_bar.addPermanentWidget(self.disk_free_label)
        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(DISK_FREE_REFRESH_MS)
        self._disk_timer.timeout.connect(self._update_disk_free)
        self._disk_timer.start()
        self._update_disk_free()

    # ---- mpv可用性 ----------------------------------------------------
    # PreviewWidget.initializeGL()はGLコンテキストが実際に有効になる
    # （最初のpaint/expose処理が走る）まで呼ばれないため、コンストラクタ
    # 直後には可用性が確定していない。connection_readyシグナル経由で
    # 判定すること（同期的にチェックしないこと）。

    def _on_mpv_connection_ready(self, connected: bool) -> None:
        if connected:
            self.preview_stack.setCurrentWidget(self.preview_widget)
            self.status_bar.showMessage(self.lang.get("messages.mpv_connected"))
            logger.info("Connected to mpv")
            self.preview_widget.set_volume(self.settings.playback_volume)
            self._apply_audio_device()
        else:
            self._show_mpv_unavailable_warning()
            self.preview_stack.setCurrentWidget(self.preview_placeholder)
            self.status_bar.showMessage(self.lang.get("messages.mpv_disconnected"))
            logger.warning("mpv is not available, starting in limited preview mode")

    def output_dir(self) -> Optional[Path]:
        """出力先: 設定の出力先、無ければ開いている録画のフォルダ。"""
        if self.settings.default_output_dir:
            return Path(self.settings.default_output_dir)
        return self.file_info.file_path.parent if self.file_info is not None else None

    def _update_disk_free(self) -> None:
        directory = self.output_dir()
        if directory is None:
            self.disk_free_label.setText("")
            return
        # まだ作られていない出力先は、存在する親フォルダのドライブで見る
        existing = next((d for d in (directory, *directory.parents) if d.exists()), None)
        try:
            free = shutil.disk_usage(existing).free if existing is not None else None
        except OSError:
            free = None
        if free is None:
            self.disk_free_label.setText(tr("main.disk_free_unknown"))
        else:
            self.disk_free_label.setText(tr("main.disk_free", free=f"{free / 1e9:.1f}"))
        self.disk_free_label.setToolTip(str(directory))

    def _apply_audio_device(self) -> None:
        if not self.preview_widget.is_connected:
            return
        name = self.settings.audio_output_device
        if not self.preview_widget.set_audio_device(name):
            logger.warning("Audio output device %s not found, using the OS default output", name)

    def _show_mpv_unavailable_warning(self) -> None:
        key = "messages.mpv_unavailable_missing" if not is_mpv_available() else "messages.mpv_unavailable_failed"
        QMessageBox.warning(self, self.lang.get("messages.mpv_unavailable_title"), self.lang.get(key))

    # ---- ファイルを開く ------------------------------------------------

    def _on_open(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            tr("main.open_video_title"),
            "",
            # 対象はテレビ録画のTSのみ(MP4等の読み込みは想定外、利用者の要件)
            tr("main.ts_files_filter"),
        )
        if not path_str:
            return
        if self.open_file(Path(path_str)):
            self.recent_files.add(Path(path_str))

    def open_file(self, path: Path, audio_track: Optional[dict] = None) -> bool:
        """動画ファイルを読み込み、プレビュー・カット編集へ反映する。
        読み込めたかどうかを返す（失敗時はエラーダイアログ表示済み）。
        audio_track はプロジェクトに保存した音声(あれば、音声の選択を聞かずにそれを使う。§4.12)。

        読み込み中はポップアップを出してほかの操作を止め、自動カットの案が出るまで続ける
        (§4.11)。ファイル情報(ffprobe・キーフレーム・走査方式)は裏のスレッドで読む。"""
        self._begin_loading()
        try:
            file_info, keyframes, scan_type, start_offset = self._run_in_background(
                lambda: self._read_file_data(path))
        except TSSmartCutError as e:
            self._end_loading()
            logger.error("[%s] File read error: %s: %s", e.code, path, e.log_message)
            QMessageBox.critical(self, tr("main.file_load_error_title"), f"[{e.code}] {e.message}")
            return False
        if self._closing:
            self._end_loading()
            return False

        logger.info("Opened file: %s", path)
        self.file_info = file_info
        self._preset_audio_track = audio_track
        self._set_audio_combo([], None)
        self.cut_manager = CutManager(file_info)
        self.bookmark_manager.clear()
        self._project_path = None
        self._project_created_at = None
        self._pending_start = None
        self._pending_end = None
        self._selected_cut_index = None

        self._keyframes = keyframes

        self._start_envelope(path, file_info.fps)
        wanted = self._proposals_wanted()
        self._start_program_info(path, wanted)
        if not wanted and self._autocut_enabled() and self.history_db is not None:
            self.proposal_status_label.setText(tr("proposals.batch_running"))

        self.setWindowTitle(f"TS_SmartCut - {path.name}")
        self._update_disk_free()
        file_info.scan_type = scan_type
        scan = tr(SCAN_LABELS.get(scan_type, "main.scan_unknown"))
        # 再エンコードはコーデック・走査方式・ビットレートを元に合わせるので、それも示す
        bitrate = f"{file_info.bitrate / 1e6:.1f}Mbps" if file_info.bitrate else tr("main.bitrate_unknown")
        self._file_info_text = (
            f"{path.name}  |  {file_info.resolution[0]}x{file_info.resolution[1]}  |  "
            f"{file_info.video_codec}/{file_info.audio_codec}  |  {scan}  |  {file_info.fps:.3f}fps  |  "
            f"{bitrate}  |  " + tr("main.frames", count=file_info.total_frames)
        )
        self._show_file_info()

        self.seek_slider.setMaximum(max(file_info.total_frames - 1, 0))
        self._update_time_label(0)
        self._update_pending_label()

        self.timeline.set_total_frames(file_info.total_frames)
        self.timeline.set_keyframes(self._keyframes)
        self._refresh_cut_list()
        self._refresh_bookmark_list()
        self._set_cut_edit_enabled(True)

        if self.preview_widget.is_connected:
            # プレビューのフレーム番号をカット処理と同じ「映像の先頭が0」に
            # そろえる(放送TSは音声が先に始まり、mpvはそこを0とする)。
            self.preview_widget.load_file(path, fps=file_info.fps, start_offset=start_offset)
            self.preview_widget.set_audio_pid(None)
            self._set_playback_controls_enabled(True)

        if wanted:
            self._set_loading_stage("program_info", tr("main.loading_program_info"))
        else:
            self._end_loading()

        # VFRではSmart CutがSimple Copyに切り替わり、カット位置がキーフレームへ
        # ずれるため、残す区間を作る前に知らせる (design doc §16.3)
        _mode, warning = resolve_processing_mode(self.settings.default_processing_mode, file_info.is_vfr)
        if warning:
            logger.warning("%s: %s", path, warning.replace("\n", " "))
            QMessageBox.warning(self, tr("main.vfr_title"), tr("main.vfr_warning"))
        return True

    # ---- 読み込み中のポップアップと操作の停止 (§4.11) ---------------------------

    def _read_file_data(self, path: Path):
        """録画を開く時の重い読み込み(裏のスレッドで呼ぶ)。"""
        file_info = self.ffmpeg.get_file_info(path)
        try:
            scan_type = self.ffmpeg.get_scan_type(path)
        except TSSmartCutError:
            scan_type = ""
        if scan_type == SCAN_PROGRESSIVE:
            # 対象は日本のテレビ放送の録画(インターレース・ソフトテレシネ)だけ。プログレッシブの H.264 では、
            # Smart Cut の Head(x264 は B フレーム無しのプログレッシブで POC の方式 2 を使う)の後ろのコピーが
            # まるごとデコードされなかった(2026-11-17 の実験)。想定外の形式は開かない(利用者の決定)
            raise ValidationError("E206", key="exceptions.progressive_not_supported", path=path)
        try:
            keyframes = self.ffmpeg.get_keyframes(path, fps=file_info.fps)
        except TSSmartCutError:
            keyframes = []
        start_offset = 0.0
        if self.preview_widget.is_connected:
            try:
                start_offset = self.ffmpeg.get_video_start_offset(path)
            except TSSmartCutError:
                start_offset = 0.0
        return file_info, keyframes, scan_type, start_offset

    @staticmethod
    def _run_in_background(fn):
        """fn を裏のスレッドで動かし、終わるまで画面の処理を回して待つ(画面を止めない)。
        fn の例外はここで投げ直す。"""
        result: dict = {}
        done = threading.Event()

        def run() -> None:
            try:
                result["value"] = fn()
            except BaseException as e:  # noqa: BLE001 - 呼び出し元で投げ直す
                result["error"] = e
            finally:
                done.set()

        threading.Thread(target=run, name="open-file", daemon=True).start()
        while not done.wait(0.02):
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if "error" in result:
            raise result["error"]
        return result["value"]

    def _ui_locked(self) -> bool:
        return self.is_encoding() or self._loading

    def _set_ui_enabled(self, enabled: bool) -> None:
        """メニュー・画面の部品(バッチリストのタブも)・ショートカット(エンコード中と読み込み中)。"""
        self.menuBar().setEnabled(enabled)
        self.centralWidget().setEnabled(enabled)
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)

    def _begin_loading(self) -> None:
        self._loading = True
        self._set_ui_enabled(False)
        if self._loading_dialog is None:
            self._loading_dialog = _LoadingDialog(self)
            self._loading_dialog.skip_requested.connect(self._on_skip_proposals)
        self._set_loading_stage("file", tr("main.loading_file"))
        self._loading_dialog.show()
        self._loading_dialog.raise_()

    def _set_loading_stage(self, stage: str, text: str, percent: Optional[float] = None) -> None:
        if self._loading_dialog is not None:
            self._loading_dialog.set_stage(stage, text, percent)

    def _end_loading(self) -> None:
        if not self._loading:
            return
        self._loading = False
        if self._loading_dialog is not None:
            self._loading_dialog.stage = ""
            self._loading_dialog.hide()
        if not self.is_encoding():
            self._set_ui_enabled(True)
        if self._pending_stale_logo is not None:
            QTimer.singleShot(0, self._show_stale_logo)
        if self._pending_logo_change is not None:
            QTimer.singleShot(0, self._ask_logo_change)

    def _on_skip_proposals(self) -> None:
        """「案を待たずに編集する」。案の準備をやめて操作できるようにする(開き直せばまた作る)。"""
        if self._proposal_stop is not None:
            self._proposal_stop.set()
        self.proposal_status_label.setText(tr("proposals.skipped"))
        logger.info("Stopped preparing automatic cut proposals (edit without waiting)")
        self._end_loading()

    def _proposals_wanted(self) -> bool:
        """録画を開いた時に自動カットの案を作るか。バッチ処理・エンコード中は変換と同時に
        動かさないため作らない(§4.11)。"""
        if not self._autocut_enabled() or self.history_db is None:
            return False
        return not (self.batch_manager.is_running or self.is_encoding())

    def _autocut_enabled(self) -> bool:
        """自動カットの案を作るか: 設定の「自動カットの案を出す」(A9)。autocut_proposals はテスト用の切り替え。"""
        return self.autocut_proposals and self.settings.autocut_proposals

    def _on_proposal_progress(self, request: int, pictures: int) -> None:
        if not self._loading or request != self._program_info_request or self.file_info is None:
            return
        total = max(self.file_info.total_frames, 1)
        percent = pictures / total * 100.0
        self._set_loading_stage("proposals", tr("proposals.preparing_percent", percent=int(min(percent, 100))), percent)

    # ---- 最近使用したファイル (design doc §4.2, §10.4) ----------------------------

    def _rebuild_recent_menu(self) -> None:
        self.recent_files.remove_missing()
        self.recent_menu.clear()
        if not self.recent_files.files:
            empty_action = self.recent_menu.addAction(self.lang.get("menu.recent_empty"))
            empty_action.setEnabled(False)
        for path in self.recent_files.files:
            action = self.recent_menu.addAction(str(path))
            action.triggered.connect(lambda _checked=False, p=path: self.open_recent_file(p))
        self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction(self.lang.get("menu.clear_recent"))
        clear_action.setEnabled(bool(self.recent_files.files))
        clear_action.triggered.connect(self._on_clear_recent_files)

    def open_recent_file(self, path: Path) -> bool:
        """履歴の項目を開く。.jsonはプロジェクト、それ以外は動画として扱う。"""
        if not path.exists():
            # メニュー表示後に削除・移動された場合
            QMessageBox.warning(
                self, self.lang.get("menu.recent_files"), self.lang.get("messages.file_not_found", path=path)
            )
            self.recent_files.remove(path)
            return False
        if path.suffix.lower() == ".json":
            return self.open_project(path)
        if not self.open_file(path):
            return False
        self.recent_files.add(path)
        return True

    # ---- ファイルのドロップ (design doc §4.6) -----------------------------------

    @staticmethod
    def _dropped_path(event) -> Optional[Path]:
        """ドロップされたのが開けるファイル1つ(TSかプロジェクト)ならそのパス。"""
        mime = event.mimeData()
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()] if mime.hasUrls() else []
        if len(paths) == 1 and paths[0].suffix.lower() in DROPPABLE_SUFFIXES:
            return paths[0]
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_path(event) is not None and not self._ui_locked():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        path = self._dropped_path(event)
        if path is None or self._ui_locked():
            event.ignore()
            return
        event.acceptProposedAction()
        # ドロップ元(ファイルマネージャー)を待たせないよう、エラーダイアログ等は
        # ドロップ処理を終えてから出す
        QTimer.singleShot(0, lambda: self.open_recent_file(path))

    def _on_clear_recent_files(self) -> None:
        self.recent_files.clear()

    # ---- プロジェクト保存・読込 (design doc §10) ---------------------------------

    def _on_save_project(self) -> None:
        if self.file_info is None or self.cut_manager is None:
            QMessageBox.information(self, self.lang.get("menu.save_project"), self.lang.get("messages.batch_no_file_open"))
            return
        if self._project_path is None:
            self._on_save_project_as()
            return
        self.save_project(self._project_path)

    def _on_save_project_as(self) -> None:
        if self.file_info is None or self.cut_manager is None:
            QMessageBox.information(self, self.lang.get("menu.save_project_as"), self.lang.get("messages.batch_no_file_open"))
            return
        default_path = self._project_path or self.file_info.file_path.with_name(
            f"{self.file_info.file_path.stem}_project.json"
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            self.lang.get("menu.save_project_as"),
            str(default_path),
            tr("main.project_filter"),
        )
        if not path_str:
            return
        path = Path(path_str)
        # 拡張子を強制することで、動画ファイル名をそのまま入力されても
        # 元ファイルを上書きしない (design doc §9.4)。
        if path.suffix.lower() != ".json":
            path = path.with_name(path.name + ".json")
        self.save_project(path)

    def save_project(self, path: Path) -> bool:
        """現在の残す区間・ブックマークをプロジェクトファイルへ保存する。"""
        if self.file_info is None or self.cut_manager is None:
            return False
        now = datetime.now()
        # キーフレームはKeyframeCache/に別途キャッシュされるため
        # プロジェクトには含めない（ファイルが肥大化するだけなので）。
        file_info = dataclasses.replace(
            self.file_info,
            cuts=list(self.cut_manager.cuts),
            bookmarks=list(self.bookmark_manager.bookmarks),
            keyframes=None,
        )
        project = Project(
            version=CURRENT_PROJECT_VERSION,
            file_info=file_info,
            settings=self.settings,
            created_at=self._project_created_at or now,
            modified_at=now,
            edit_events=list(self.cut_manager.edit_events),
        )
        try:
            ProjectManager.save_project(project, path)
        except TSSmartCutError as e:
            logger.error("[%s] Project save error: %s: %s", e.code, path, e.log_message)
            QMessageBox.critical(self, self.lang.get("menu.save_project"), f"[{e.code}] {e.message}")
            return False
        self._project_path = path
        self._project_created_at = project.created_at
        self.recent_files.add(path)
        logger.info("Saved project: %s", path)
        self.status_bar.showMessage(self.lang.get("messages.project_saved", path=path))
        return True

    def _on_open_project(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            self.lang.get("menu.open_project"),
            "",
            tr("main.project_filter") + ";;" + tr("main.all_files_filter"),
        )
        if not path_str:
            return
        self.open_project(Path(path_str))

    def open_project(self, path: Path) -> bool:
        """プロジェクトファイルを読み込み、対象の動画を開いて残す区間・
        ブックマークを復元する。

        動画情報はプロジェクト内の値ではなく、実ファイルを改めてffprobeした
        結果を使う。プロジェクト内の設定(settings)はアプリ全体の設定を
        上書きしない（テーマ・言語・出力先等が勝手に変わるのを避けるため）。"""
        try:
            project = ProjectManager.load_project(path)
        except TSSmartCutError as e:
            logger.error("[%s] Project load error: %s: %s", e.code, path, e.log_message)
            QMessageBox.critical(self, self.lang.get("menu.open_project"), f"[{e.code}] {e.message}")
            return False

        saved = project.file_info
        if not saved.file_path.exists():
            logger.error("[E001] The project's video file was not found: %s", saved.file_path)
            QMessageBox.critical(
                self,
                self.lang.get("menu.open_project"),
                f"[E001] {self.lang.get('messages.file_not_found', path=saved.file_path)}",
            )
            return False

        if not self.open_file(saved.file_path, audio_track=saved.audio_track):
            return False

        if self.file_info.file_size != saved.file_size or self.file_info.total_frames != saved.total_frames:
            logger.warning("The video file may have changed since the project was saved: %s", saved.file_path)
            QMessageBox.warning(self, self.lang.get("menu.open_project"), self.lang.get("messages.project_file_changed"))

        # add_cut()を通すことで範囲外・重複の区間を検証する。復元はユーザー
        # 操作ではないため、Undo履歴には残さない。
        skipped = 0
        for cut in saved.cuts:
            try:
                self.cut_manager.add_cut(cut.start_frame, cut.end_frame, cut.label, is_excluded=cut.is_excluded)
            except ValidationError as e:
                logger.warning("[%s] Could not restore a kept range: %s-%s: %s", e.code, cut.start_frame, cut.end_frame, e.log_message)
                skipped += 1
        self.cut_manager.history.clear()
        self.cut_manager.history_index = -1
        self.cut_manager.edit_events = list(project.edit_events)
        if skipped:
            QMessageBox.warning(
                self, self.lang.get("menu.open_project"), self.lang.get("messages.project_cuts_skipped", count=skipped)
            )

        self.bookmark_manager.bookmarks = sorted(
            (b for b in saved.bookmarks if 0 <= b.frame < self.file_info.total_frames),
            key=lambda b: b.frame,
        )

        self._refresh_cut_list()
        self._refresh_bookmark_list()
        self._project_path = path
        self._project_created_at = project.created_at
        self.recent_files.add(path)
        logger.info("Loaded project: %s", path)
        self.status_bar.showMessage(self.lang.get("messages.project_loaded", path=path))
        return True

    # ---- 再生制御 -----------------------------------------------------------

    def _set_playback_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.back5s_button,
            self.frame_back_button,
            self.play_pause_button,
            self.frame_forward_button,
            self.forward5s_button,
            self.seek_slider,
            self.speed_combo,
        ):
            widget.setEnabled(enabled)
        self._update_jump_buttons()

    def _on_toggle_pause(self) -> None:
        if self._muted_for_stepping:
            self._muted_for_stepping = False
            self.preview_widget.set_mute(self._muted)
        self.preview_widget.command(["cycle", "pause"])

    def _on_frame_forward(self) -> None:
        self._silence_for_stepping()
        self.preview_widget.command(["frame-step"])

    def _on_frame_back(self) -> None:
        self._silence_for_stepping()
        self.preview_widget.command(["frame-back-step"])

    def _on_forward_5s(self) -> None:
        self.preview_widget.command(["seek", 5, "relative"])

    def _on_back_5s(self) -> None:
        self.preview_widget.command(["seek", -5, "relative"])

    def _on_speed_changed(self, text: str) -> None:
        try:
            speed = float(text)
        except ValueError:
            return
        self.preview_widget.set_playback_speed(speed)

    def _on_speed_step(self, direction: int) -> None:
        """design doc §4.5: [ ] キーで再生速度を段階的に変更する。"""
        current_index = PLAYBACK_SPEEDS.index(self.speed_combo.currentText())
        new_index = max(0, min(len(PLAYBACK_SPEEDS) - 1, current_index + direction))
        self.speed_combo.setCurrentText(PLAYBACK_SPEEDS[new_index])

    def _on_prev_keyframe(self) -> None:
        """design doc §4.5: < キーで前のキーフレームへ移動する。"""
        if not self._keyframes:
            return
        frame = self.preview_widget.get_current_frame()
        if frame is None:
            return
        candidates = [k for k in self._keyframes if k < frame]
        if candidates:
            self.preview_widget.seek_frame(max(candidates))

    def _on_next_keyframe(self) -> None:
        """design doc §4.5: > キーで次のキーフレームへ移動する。"""
        if not self._keyframes:
            return
        frame = self.preview_widget.get_current_frame()
        if frame is None:
            return
        candidates = [k for k in self._keyframes if k > frame]
        if candidates:
            self.preview_widget.seek_frame(min(candidates))

    def _on_timeline_clicked(self, frame: int) -> None:
        if self.file_info is not None:
            self.preview_widget.seek_frame(frame)

    def _on_toggle_waveform(self, checked: bool) -> None:
        self.settings.show_waveform = checked
        self.waveform_view.setVisible(checked)

    def _tab_pages(self):
        return ((self.cut_list_page, "show_cut_list_tab"),
                (self.bookmark_list, "show_bookmark_tab"),
                (self.batch_list_view, "show_batch_tab"))

    def _apply_tab_visibility(self) -> None:
        """設定どおりにタブを出す・隠す。全部隠したらタブの場所ごと消す(プレビューが広がる)。"""
        for page, key in self._tab_pages():
            self.lists_tabs.setTabVisible(self.lists_tabs.indexOf(page), getattr(self.settings, key))
        self.lists_tabs.setVisible(any(getattr(self.settings, key) for _page, key in self._tab_pages()))

    def _on_toggle_tab(self, key: str, checked: bool) -> None:
        setattr(self.settings, key, checked)
        self._apply_tab_visibility()

    def _on_batch_title_changed(self, title: str) -> None:
        self.lists_tabs.setTabText(self.lists_tabs.indexOf(self.batch_list_view), title)

    def _on_batch_column_widths_changed(self, widths: list) -> None:
        self.settings.batch_column_widths = list(widths)

    # ---- 音声波形 ----------------------------------------------------------

    def _start_envelope(self, path: Path, fps: float) -> None:
        self._envelope = None
        self._envelope_request += 1
        request = self._envelope_request
        self.waveform_view.set_loading(fps)
        ffmpeg_path, ffprobe_path = self.ffmpeg.ffmpeg_path, self.ffmpeg.ffprobe_path

        def work() -> None:
            try:
                envelope = generate_envelope(path, ffmpeg_path, ffprobe_path)
            except TSSmartCutError as e:
                logger.warning("Could not make the audio waveform: %s", e)
                envelope = None
            self._envelope_bridge.ready.emit(request, envelope, fps)

        threading.Thread(target=work, daemon=True).start()

    def _on_envelope_ready(self, request: int, envelope, fps: float) -> None:
        if request != self._envelope_request:
            return  # その後で別のファイルを開いた
        self._envelope = envelope
        self.waveform_view.set_envelope(envelope, fps)

    # ---- 番組情報 ------------------------------------------------------------

    # 自動カットの案を出すか(テストで止める。A9 で設定にする)
    autocut_proposals = True

    def _start_program_info(self, path: Path, want_proposals: Optional[bool] = None) -> None:
        self.program_info = None
        self.registration = None
        self._program_info_request += 1
        request = self._program_info_request
        db = self.history_db
        if self._proposal_stop is not None:
            self._proposal_stop.set()
        stop = self._proposal_stop = _ProposalStop(self)
        self.proposal_status_label.setText("")
        fps = self.file_info.fps if self.file_info is not None else 0.0
        ffmpeg_path, ffprobe_path = self.ffmpeg.ffmpeg_path, self.ffmpeg.ffprobe_path
        if want_proposals is None:
            want_proposals = self._autocut_enabled() and db is not None
        bridge = self._program_info_bridge
        notify_logo_change = self.settings.notify_logo_change
        # 案の方式(A8)。読み込みのスレッドでは設定を読まないので、ここで写しを取る
        autocut_method = self.settings.autocut_method
        station_methods = dict(self.settings.autocut_station_methods)

        def work() -> None:
            # 読めなくても録画を開く・カットする操作は止めない(ログに残すだけ)
            try:
                info = read_program_info(path)
            except Exception:
                logger.warning("Could not read the programme information: %s", path, exc_info=True)
                info = None
            bridge.ready.emit(request, info)
            # 局ロゴのフォルダ (Logo/) の新しい・変わった .lgd を取り込み、この局の取り込んだロゴが古くないか見る
            service_id = getattr(info, "service_id", None)
            if db is not None and service_id is not None:
                try:
                    sync_logo_folder(db)
                    stale = check_imported_logo(db, getattr(info, "original_network_id", None), service_id)
                    if stale is not None:
                        bridge.stale_logo.emit(request, stale)
                except Exception:  # noqa: BLE001 - 取り込めなくても録画は開く
                    logger.warning("Could not update the imported station logos", exc_info=True)
                # この局のロゴが変わった可能性 (A9。設定で切れる)
                if notify_logo_change:
                    try:
                        change = detect_logo_change(db, getattr(info, "original_network_id", None), service_id)
                        if change is not None:
                            bridge.logo_change.emit(request, (change, getattr(info, "service_name", "") or ""))
                    except Exception:  # noqa: BLE001
                        logger.warning("Could not check the station logo for a change", exc_info=True)
            # 同じ動画(先頭 16MB のハッシュとサイズ)が記録 DB に登録済みか
            registration = None
            if db is not None:
                try:
                    registration = db.get_video_registration(compute_file_hash(path), path.stat().st_size)
                    bridge.registered.emit(request, registration)
                except Exception:
                    logger.warning("Could not check the history DB registration: %s", path, exc_info=True)
            # 自動カットの案(A6)。番組情報のある TS だけ(無ければ ready の側で読み込みを終える)
            if not want_proposals or db is None or info is None or fps <= 0:
                return
            try:
                proposals = build_file_proposals(db, path, fps, info, registration, stop_event=stop,
                                                 ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path,
                                                 progress=lambda n: bridge.progress.emit(request, n),
                                                 default_method=autocut_method, station_methods=station_methods)
            except Exception:  # noqa: BLE001 - 案が出ないだけにする
                logger.warning("Could not make automatic cut proposals: %s", path, exc_info=True)
                bridge.proposals.emit(request, None, tr("proposals.failed"))
                return
            if proposals is None:
                if not stop.event.is_set():
                    bridge.proposals.emit(request, None, tr("proposals.stopped_busy"))
                return
            bridge.proposals.emit(request, proposals, "")

        threading.Thread(target=work, daemon=True).start()

    def _on_program_info_ready(self, request: int, info: Optional[ProgramInfo]) -> None:
        if request != self._program_info_request:
            return  # その後で別のファイルを開いた
        self.program_info = info
        self._apply_audio_tracks(info)
        if self._loading and self._loading_dialog is not None and self._loading_dialog.stage == "program_info":
            if info is None or self.file_info is None or self.file_info.fps <= 0:
                self._end_loading()  # 放送の録画でない: 案は作らない
            else:
                self.proposal_status_label.setText(tr("proposals.preparing"))
                self._set_loading_stage("proposals", tr("proposals.preparing"), 0.0)
        if info is not None:
            logger.info(
                "Programme information: %s \"%s\" broadcast %s recorded %s - %s",
                info.service_name, info.event.name if info.event else "",
                info.event.start if info.event else None, info.recording_start, info.recording_end,
            )
        self._show_file_info()

    def _on_proposals_ready(self, request: int, proposals, message: str) -> None:
        if request != self._program_info_request or self.cut_manager is None:
            return
        self._end_loading()
        if proposals is None:
            self.proposal_status_label.setText(message)
            return
        self.cut_manager.set_proposals(proposals)
        count = len(self.cut_manager.pending_proposals)
        self.proposal_status_label.setText(tr("proposals.count", count=count) if count else tr("proposals.none"))
        logger.info("Made %d automatic cut proposals", count)
        self._refresh_cut_list()

    # ---- 音声の選択 (設計書 §4.12) ------------------------------------------------------

    def _apply_audio_tracks(self, info: Optional[ProgramInfo]) -> None:
        """番組情報から選べる音声の一覧を作り、音声を決める。プロジェクトに保存した音声があればそれ、
        無ければ 1 つ目。音声が 2 つ以上あり、保存した音声が無ければ聞く。"""
        if self.file_info is None:
            return
        tracks = audio_tracks(info)
        preset = AudioTrack.from_dict(self._preset_audio_track) if self._preset_audio_track else None
        self._preset_audio_track = None
        self.file_info.audio_tracks = [t.to_dict() for t in tracks]
        chosen = next((t for t in tracks if same_track(t, preset)), None) if preset else None
        self._set_audio_combo(tracks, chosen or (tracks[0] if tracks else None))
        self._use_audio_track(chosen or (tracks[0] if tracks else None))
        if len(tracks) >= 2:
            logger.info("Audio tracks: %s", ", ".join(f"PID 0x{t.pid:X} tag {t.component_tag}" for t in tracks))
            if chosen is None:
                QTimer.singleShot(0, lambda request=self._program_info_request: self._ask_audio_track(request))

    def _ask_audio_track(self, request: int) -> None:
        """音声の選択の画面(モーダル)。案づくりは裏で続ける。読み込み中のポップアップはその間だけ隠す。"""
        if request != self._program_info_request or self.file_info is None or len(self.file_info.audio_tracks) < 2:
            return
        tracks = [AudioTrack.from_dict(t) for t in self.file_info.audio_tracks]
        fps = self.file_info.fps
        return_frame = self.preview_widget.get_current_frame() if self.preview_widget.is_connected else None
        window = self._program_window_start()
        start = int(round((window + LISTEN_AFTER_PROGRAMME_START) * fps)) if window is not None else 0
        loading_shown = self._loading_dialog is not None and self._loading_dialog.isVisible()
        if loading_shown:
            self._loading_dialog.hide()
        dialog = AudioSelectDialog(tracks, self.file_info.total_frames, fps, start, _AudioDialogPreview(self), self)
        dialog.exec()
        track = dialog.selected_track()
        if loading_shown and self._loading and self._loading_dialog is not None:
            self._loading_dialog.show()
        self.preview_widget.command(["set", "pause", "yes"])
        self.preview_widget.set_mute(self._muted)
        if request != self._program_info_request or self.file_info is None:
            return  # 画面を開いている間に別のファイルを開いた(閉じた)
        self._set_audio_combo(tracks, track)
        self._use_audio_track(track)
        logger.info("Chose audio track %d (PID 0x%X)", track.index + 1, track.pid)
        if return_frame is not None:
            self.preview_widget.seek_frame(return_frame)

    def _program_window_start(self) -> Optional[float]:
        """番組の放送開始の位置(映像の先頭からの秒)。分からなければ None。"""
        from ts_smartcut.autocut.proposals import window_from_program_info

        window = window_from_program_info(self.program_info)
        return max(window.start, 0.0) if window is not None else None

    def _set_audio_combo(self, tracks: List[AudioTrack], chosen: Optional[AudioTrack]) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        for track in tracks:
            self.audio_combo.addItem(track.label(), track.to_dict())
        if chosen is not None:
            self.audio_combo.setCurrentIndex(chosen.index)
        self.audio_combo.blockSignals(False)
        self.audio_combo.setEnabled(len(tracks) >= 2)

    def _use_audio_track(self, track: Optional[AudioTrack]) -> None:
        """その録画の音声にする: 出力(バッチ・エンコード開始)とプレビュー。"""
        if self.file_info is None:
            return
        self.file_info.audio_track = track.to_dict() if track is not None else None
        self.preview_widget.set_audio_pid(track.pid if track is not None else None)

    def _on_audio_combo_changed(self, index: int) -> None:
        data = self.audio_combo.itemData(index)
        if data is None or self.file_info is None:
            return
        track = AudioTrack.from_dict(data)
        self._use_audio_track(track)
        logger.info("Changed the audio track to %d (PID 0x%X)", track.index + 1, track.pid)

    # ---- 自動カットの案の承認・棄却 (設計書 §31.6、計画 A6) ----------------------

    def _selected_proposal(self):
        if self.cut_manager is None:
            return None
        indexes = self.cut_list.selectedIndexes()
        if not indexes:
            return None
        row = indexes[0].row() - len(self.cut_manager.cuts)
        pending = self.cut_manager.pending_proposals
        return pending[row] if 0 <= row < len(pending) else None

    def _on_accept_proposal(self, proposal=None) -> None:
        proposal = proposal or self._selected_proposal()
        if proposal is None or self.cut_manager is None:
            return
        try:
            cut = self.cut_manager.accept_proposal(proposal)
        except ValidationError as e:
            QMessageBox.warning(self, tr("proposals.accept_title"), f"[{e.code}] {e.message}")
            return
        logger.info("Accepted an automatic cut proposal: %s-%s", proposal.start_frame, proposal.end_frame)
        self._refresh_cut_list()
        # 承認した区間を選んだままにする(続けて S / E で直せる。開始へは移動しない)
        self._select_cut(next(i for i, c in enumerate(self.cut_manager.cuts) if c is cut))

    def _on_reject_proposal(self, proposal=None) -> None:
        proposal = proposal or self._selected_proposal()
        if proposal is None or self.cut_manager is None:
            return
        self.cut_manager.reject_proposal(proposal)
        logger.info("Rejected an automatic cut proposal: %s-%s", proposal.start_frame, proposal.end_frame)
        self._refresh_cut_list()
        # 次の案を選んでその開始へ(行のクリックと同じ)。次の案が無ければ選ばず、位置もそのまま(2026-10-17)
        pending = self.cut_manager.pending_proposals
        following = [i for i, p in enumerate(pending) if p.start_frame > proposal.start_frame]
        if following:
            index = min(following, key=lambda i: pending[i].start_frame)
            self._select_row(len(self.cut_manager.cuts) + index)
            if self.file_info is not None:
                self._jump_to_frame(pending[index].start_frame)

    def _on_accept_all_proposals(self) -> None:
        if self.cut_manager is None or not self.cut_manager.pending_proposals:
            return
        before = list(self.cut_manager.pending_proposals)
        skipped = self.cut_manager.accept_all_proposals()
        logger.info("Accepted all automatic cut proposals (%d not accepted because they overlap)", len(skipped))
        self._refresh_cut_list()
        # 承認したうちいちばん後ろの区間を選び、その最後のフレームへ(2026-10-17)
        accepted = [p for p in before if p not in skipped]
        if accepted:
            last = max(accepted, key=lambda p: p.end_frame)
            self._select_cut(next(i for i, c in enumerate(self.cut_manager.cuts)
                                  if (c.start_frame, c.end_frame) == (last.start_frame, last.end_frame)))
            if self.file_info is not None:
                self._jump_to_frame(last.end_frame)
        if skipped:
            QMessageBox.warning(self, tr("proposals.accept_title"), tr("proposals.overlap_skipped", count=len(skipped)))

    def _update_proposal_buttons(self) -> None:
        pending = bool(self.cut_manager is not None and self.cut_manager.pending_proposals)
        selected = self._selected_proposal() is not None
        self.accept_proposal_button.setEnabled(selected)
        self.reject_proposal_button.setEnabled(selected)
        self.accept_all_proposals_button.setEnabled(pending)

    def _on_registration_ready(self, request: int, registration: Optional[dict]) -> None:
        if request != self._program_info_request:
            return
        self.registration = registration
        if registration is not None:
            logger.info("Video already in the automatic cut history: id=%s registered %s", registration["id"], registration["created_at"])
        self._show_file_info()

    @staticmethod
    def _local_time(iso: Optional[str]) -> str:
        if not iso:
            return "-"
        try:
            return f"{datetime.fromisoformat(iso).astimezone():%Y/%m/%d %H:%M}"
        except ValueError:
            return iso

    def _show_file_info(self) -> None:
        """ファイル情報の行。番組情報が読めたら、先頭に局名と番組名を出す。
        自動カットの記録に登録済みの動画なら、さらに先頭に「登録済み」を出す。"""
        info = self.program_info
        program = ""
        lines: List[str] = []
        if self.registration is not None:
            reg = self.registration
            program = tr("main.registered") + "  |  "
            lines.append(tr("main.registered_detail", created=self._local_time(reg["created_at"]),
                            updated=self._local_time(reg["updated_at"]), count=reg["cut_count"]))
        if info is not None and (info.service_name or info.event is not None):
            name = info.event.name if info.event is not None else ""
            program += "  ".join(part for part in (info.service_name, name) if part) + "  |  "
            lines += [part for part in (info.service_name, name) if part]
            event = info.event
            if event is not None and event.start is not None:
                start = f"{event.start:%Y/%m/%d %H:%M}"
                if event.end is not None:
                    lines.append(tr("main.broadcast_range", start=start, end=f"{event.end:%H:%M}"))
                else:
                    lines.append(tr("main.broadcast", start=start))
            if info.recording_start is not None and info.recording_end is not None:
                lines.append(tr("main.recording_range", start=f"{info.recording_start:%Y/%m/%d %H:%M:%S}",
                                end=f"{info.recording_end:%H:%M:%S}"))
            if event is not None and event.description:
                lines.append(event.description)
        if lines:
            lines.append("")
        if self._file_info_text:
            lines.append(self._file_info_text)
        self.file_info_label.setText(program + self._file_info_text)
        self.file_info_label.setToolTip("\n".join(lines))

    # ---- 音 ----------------------------------------------------------------

    def _update_mute_button(self) -> None:
        self.mute_button.setText(tr("main.mute_on") if self._muted else tr("main.mute_off"))
        self.mute_button.setToolTip(self._with_key(tr("main.mute_tip"), "mute"))

    def _on_mute_toggled(self, checked: bool) -> None:
        self._muted = checked
        self._muted_for_stepping = False
        self._update_mute_button()
        self.preview_widget.set_mute(checked)

    def _on_toggle_mute_key(self) -> None:
        self.mute_button.setChecked(not self.mute_button.isChecked())

    def _silence_for_stepping(self) -> None:
        """フレーム送りでは音を鳴らさない(mpvのframe-stepは1フレーム分再生する)。
        次に再生を始める時に元へ戻す。"""
        if not self._muted and not self._muted_for_stepping:
            self.preview_widget.set_mute(True)
            self._muted_for_stepping = True

    # ---- カット編集 (design doc §4.4, §7) ------------------------------------

    def _set_cut_edit_enabled(self, enabled: bool) -> None:
        for widget in (
            self.start_encode_button,
            self.add_to_batch_button,
            self.set_start_button,
            self.set_end_button,
            self.add_cut_button,
            self.edit_cut_button,
            self.delete_cut_button,
        ):
            widget.setEnabled(enabled)
        for action in (
            self.set_start_action,
            self.set_end_action,
            self.add_cut_action,
            self.delete_cut_action,
            self.edit_cut_action,
            self.add_bookmark_action,
            self.remove_bookmark_action,
            self.clear_all_action,
            self.undo_action,
            self.redo_action,
            self.save_project_action,
            self.save_project_as_action,
        ):
            action.setEnabled(enabled)

    def _update_pending_label(self) -> None:
        """区間を選んでいる時は、S / E がその区間を直すことを出す(設計書 §4.3)。"""
        if self.cut_manager is not None and self._selected_cut_index is not None:
            cut = self.cut_manager.cuts[self._selected_cut_index]
            key = self._key_text("clear_selection")
            if key:
                text = tr("cuts.editing_selected", start=cut.start_frame, end=cut.end_frame, shortcut=key)
            else:
                text = tr("cuts.editing_selected_no_key", start=cut.start_frame, end=cut.end_frame)
            self.pending_label.setText(text)
            return
        start_text = str(self._pending_start) if self._pending_start is not None else "-"
        end_text = str(self._pending_end) if self._pending_end is not None else "-"
        self.pending_label.setText(f"Start: {start_text} / End: {end_text}")

    def _on_set_start(self) -> None:
        self._set_frame_of_range(start=True)

    def _on_set_end(self) -> None:
        self._set_frame_of_range(start=False)

    def _set_frame_of_range(self, start: bool) -> None:
        """S / E (設計書 §4.3、2026-10-14): 残す区間を選んでいればその区間の開始 / 終了を今の
        フレームに書き換え、選んでいなければ新しい区間の開始 / 終了として覚える。未判断の案の行では
        書き換えず、先に承認するよう知らせる。"""
        if self.cut_manager is None or self._ui_locked():
            return
        frame = self.preview_widget.get_current_frame()
        if frame is None:
            return
        if self._selected_proposal() is not None:
            self.status_bar.showMessage(tr("messages.proposal_accept_first"), STATUS_MESSAGE_MS)
            return
        if self._selected_cut_index is None:
            if start:
                self._pending_start = frame
            else:
                self._pending_end = frame
            self._update_pending_label()
            return
        index = self._selected_cut_index
        cut = self.cut_manager.cuts[index]
        new_start, new_end = (frame, cut.end_frame) if start else (cut.start_frame, frame)
        if (new_start, new_end) == (cut.start_frame, cut.end_frame):
            return
        try:
            self.cut_manager.edit_cut(index, new_start, new_end)
        except ValidationError as e:
            logger.warning("[%s] Could not change a kept range: %s", e.code, e.log_message)
            QMessageBox.warning(self, tr("cuts.edit_title"), f"[{e.code}] {e.message}")
            return
        logger.info("Set the %s of a kept range: %s-%s -> %s-%s", "start" if start else "end",
                    cut.start_frame, cut.end_frame, new_start, new_end)
        self._refresh_cut_list()
        # 書き換えた後も同じ区間を選んだままにする(並びが変わっても。開始へは移動しない)
        self._select_cut(next(i for i, c in enumerate(self.cut_manager.cuts)
                              if (c.start_frame, c.end_frame) == (new_start, new_end)))
        key = "messages.cut_start_set" if start else "messages.cut_end_set"
        self.status_bar.showMessage(tr(key, frame=frame), STATUS_MESSAGE_MS)

    def _select_cut(self, index: int) -> None:
        """一覧の残す区間の行を選ぶ(行のクリックと違い、開始へは移動しない)。"""
        self._select_row(index)

    def _select_row(self, row: int) -> None:
        """一覧の行(残す区間の後に未判断の案が並ぶ)を選ぶ。移動はしない。"""
        item = self.cut_list.topLevelItem(row)
        if item is not None:
            self.cut_list.setCurrentItem(item)

    def eventFilter(self, watched, event) -> bool:
        """残す区間の一覧の何も無い所のクリックで選択を外す(設計書 §4.3)。"""
        cut_list = getattr(self, "cut_list", None)
        if (cut_list is not None and watched is cut_list.viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and cut_list.itemAt(event.position().toPoint()) is None):
            self._on_clear_cut_selection()
        return super().eventFilter(watched, event)

    def _on_clear_cut_selection(self) -> None:
        """Esc・一覧の何も無い所のクリック: 選択を外す(S / E が新しい区間用に戻る)。"""
        if self._ui_locked():
            return
        self.cut_list.clearSelection()
        self.cut_list.setCurrentItem(None)

    def _selected_row_range(self) -> Optional[Tuple[int, int]]:
        """選んだ行(残す区間・未判断の案)の (開始, 終了)。"""
        if self.cut_manager is None:
            return None
        if self._selected_cut_index is not None:
            cut = self.cut_manager.cuts[self._selected_cut_index]
            return cut.start_frame, cut.end_frame
        proposal = self._selected_proposal()
        if proposal is not None:
            return proposal.start_frame, proposal.end_frame
        return None

    def _update_jump_buttons(self) -> None:
        enabled = (self.file_info is not None and self.seek_slider.isEnabled()
                   and self._selected_row_range() is not None)
        self.jump_start_button.setEnabled(enabled)
        self.jump_end_button.setEnabled(enabled)

    def _on_jump_to_selected_start(self) -> None:
        self._jump_to_selected(start=True)

    def _on_jump_to_selected_end(self) -> None:
        self._jump_to_selected(start=False)

    def _jump_to_selected(self, start: bool) -> None:
        """Home / End: 選んだ行の開始・終了のフレームへ(設計書 §4.3、2026-10-14)。"""
        if self.file_info is None or self._ui_locked() or not self.seek_slider.isEnabled():
            return
        selected = self._selected_row_range()
        if selected is not None:
            self._jump_to_frame(selected[0] if start else selected[1])

    def _on_add_cut(self) -> None:
        if self.cut_manager is None:
            return
        if self._pending_start is None or self._pending_end is None:
            QMessageBox.warning(self, tr("cuts.add_title"), tr("messages.add_cut_missing_start_end"))
            return
        try:
            self.cut_manager.add_cut(self._pending_start, self._pending_end)
        except ValidationError as e:
            logger.warning("[%s] Could not add a kept range: %s", e.code, e.log_message)
            QMessageBox.warning(self, tr("cuts.add_title"), f"[{e.code}] {e.message}")
            return
        logger.info("Added a kept range: %s-%s", self._pending_start, self._pending_end)
        self._pending_start = None
        self._pending_end = None
        self._update_pending_label()
        self._refresh_cut_list()

    def _on_delete_cut(self) -> None:
        if self.cut_manager is None or self._selected_cut_index is None:
            return
        cut = self.cut_manager.cuts[self._selected_cut_index]
        self.cut_manager.remove_cut(self._selected_cut_index)
        logger.info("Deleted a kept range: %s-%s", cut.start_frame, cut.end_frame)
        self._selected_cut_index = None
        self._refresh_cut_list()

    def _on_edit_cut(self) -> None:
        if self.cut_manager is None or self._selected_cut_index is None:
            QMessageBox.information(self, tr("cuts.edit_title"), tr("messages.edit_cut_no_selection"))
            return
        cut = self.cut_manager.cuts[self._selected_cut_index]
        new_start, ok = QInputDialog.getInt(
            self, tr("cuts.edit_title"), tr("cuts.start_frame"), cut.start_frame, 0, self.file_info.total_frames
        )
        if not ok:
            return
        new_end, ok = QInputDialog.getInt(
            self, tr("cuts.edit_title"), tr("cuts.end_frame"), cut.end_frame, 0, self.file_info.total_frames
        )
        if not ok:
            return
        try:
            self.cut_manager.edit_cut(self._selected_cut_index, new_start, new_end)
        except ValidationError as e:
            logger.warning("[%s] Could not edit a kept range: %s", e.code, e.log_message)
            QMessageBox.warning(self, tr("cuts.edit_title"), f"[{e.code}] {e.message}")
            return
        logger.info("Edited a kept range: %s-%s", new_start, new_end)
        self._refresh_cut_list()

    def _on_undo(self) -> None:
        if self.cut_manager is not None and self.cut_manager.undo():
            self._refresh_cut_list()
            self._refresh_bookmark_list()  # 「すべてクリア」の取り消しでブックマークも戻る

    def _on_redo(self) -> None:
        if self.cut_manager is not None and self.cut_manager.redo():
            self._refresh_cut_list()
            self._refresh_bookmark_list()

    def _on_clear_all(self) -> None:
        """残す区間とブックマークをすべて消す (design doc §4.2)。Ctrl+Zで戻せる。"""
        if self.cut_manager is None or (not self.cut_manager.cuts and not self.bookmark_manager.bookmarks):
            return
        answer = QMessageBox.question(
            self,
            self.lang.get("menu.clear_all"),
            self.lang.get(
                "messages.clear_all_confirm" if self._key_text("undo") else "messages.clear_all_confirm_no_key",
                cuts=len(self.cut_manager.cuts),
                bookmarks=len(self.bookmark_manager.bookmarks),
                shortcut=self._key_text("undo"),
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.cut_manager.clear_all(self.bookmark_manager)
        self._selected_cut_index = None
        self._refresh_cut_list()
        self._refresh_bookmark_list()
        logger.info("Cleared all kept ranges and bookmarks")

    def _refresh_cut_list(self) -> None:
        self.cut_list.clear()
        self._update_cut_list_title()
        if self.cut_manager is None:
            self.timeline.set_cuts([])
            self.waveform_view.set_cuts([])
            self.timeline.set_proposals([])
            self.waveform_view.set_proposals([])
            self._update_proposal_buttons()
            return
        for cut in self.cut_manager.cuts:
            label = cut.label or ""
            if cut.is_excluded:
                label = tr("cuts.label_excluded", label=label) if label else tr("cuts.excluded")
            item = QTreeWidgetItem([str(cut.start_frame), str(cut.end_frame), label])
            self.cut_list.addTopLevelItem(item)
        # 未判断の案(青)。理由と確信度は列とツールチップに
        proposal_color = QColor(PROPOSAL_COLOR)
        proposal_color.setAlpha(90)
        for proposal in self.cut_manager.pending_proposals:
            text = tr("proposals.list_label", confidence=f"{proposal.confidence:.1f}", reason=proposal.reason)
            item = QTreeWidgetItem([str(proposal.start_frame), str(proposal.end_frame), text])
            tooltip = (tr("proposals.tooltip", confidence=f"{proposal.confidence:.1f}") + "\n"
                       + proposal.reason.replace(" / ", "\n"))
            for column in range(3):
                item.setBackground(column, proposal_color)
                item.setToolTip(column, tooltip)
            self.cut_list.addTopLevelItem(item)
        ranges = [(p.start_frame, p.end_frame) for p in self.cut_manager.pending_proposals]
        self.timeline.set_cuts(self.cut_manager.cuts)
        self.waveform_view.set_cuts(self.cut_manager.cuts)
        self.timeline.set_proposals(ranges)
        self.waveform_view.set_proposals(ranges)
        self.timeline.set_selected_cut_index(None)
        self._selected_cut_index = None
        self._update_proposal_buttons()

    def _update_cut_list_title(self) -> None:
        """見出しに残す区間の合計時間(確定)と、自動カットの案の合計時間(計算)を出す
        (design doc §4.8)。案が無い時の計算は「―」。"""
        seconds = 0.0
        computed = "―"
        if self.cut_manager is not None and self.file_info is not None and self.file_info.fps > 0:
            seconds = self.cut_manager.total_cut_frames / self.file_info.fps
            proposal_frames = self.cut_manager.total_proposal_frames
            if proposal_frames is not None:
                computed = format_time(proposal_frames / self.file_info.fps)
        self.cut_list_title.setText(self.lang.get(
            "labels.cut_list_title_with_total", total=format_time(seconds), computed=computed))

    def _on_cut_selection_changed(self) -> None:
        indexes = self.cut_list.selectedIndexes()
        cuts = len(self.cut_manager.cuts) if self.cut_manager is not None else 0
        if not indexes or indexes[0].row() >= cuts:
            self._selected_cut_index = None  # 案の行は残す区間の操作の対象にしない
        else:
            self._selected_cut_index = indexes[0].row()
        self.timeline.set_selected_cut_index(self._selected_cut_index)
        self._update_proposal_buttons()
        self._update_jump_buttons()
        self._update_pending_label()

    def _on_cut_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """残す区間の行なら区間編集を開く。未判断の案の行は何もしない(クリックで先頭へ移動済み)。"""
        if self.cut_manager is None or self._ui_locked():
            return
        index = self.cut_list.indexOfTopLevelItem(item)
        if not (0 <= index < len(self.cut_manager.cuts)):
            return
        self._selected_cut_index = index
        self._on_edit_cut()

    def _on_cut_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if self.file_info is None or self._ui_locked():
            return
        try:
            frame = int(item.text(0))
        except ValueError:
            return
        self._jump_to_frame(frame)

    def _jump_to_frame(self, frame: int) -> None:
        """プレビュー・シークバー・時間表示・タイムラインを frame へ(mpv が無くても動かす)。"""
        frame = max(0, min(frame, max(self.file_info.total_frames - 1, 0)))
        try:
            self.preview_widget.seek_frame(frame)
        except TSSmartCutError as e:
            logger.debug("Could not move to the start of the range: %s", e.log_message)
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(frame)
        self.seek_slider.blockSignals(False)
        self._update_time_label(frame)
        self.timeline.set_current_frame(frame)
        self.waveform_view.set_current_frame(frame)

    def _on_cut_context_menu(self, position) -> None:
        item = self.cut_list.itemAt(position)
        if item is None:
            return
        index = self.cut_list.indexOfTopLevelItem(item)
        cuts = len(self.cut_manager.cuts) if self.cut_manager is not None else 0
        if index >= cuts and self.cut_manager is not None:
            pending = self.cut_manager.pending_proposals
            if index - cuts < len(pending):
                proposal = pending[index - cuts]
                menu = QMenu(self)
                accept = menu.addAction(tr("proposals.accept"))
                reject = menu.addAction(tr("proposals.reject"))
                chosen = menu.exec(self.cut_list.viewport().mapToGlobal(position))
                if chosen is accept:
                    self._on_accept_proposal(proposal)
                elif chosen is reject:
                    self._on_reject_proposal(proposal)
            return
        self._show_cut_context_menu(index, self.cut_list.viewport().mapToGlobal(position))

    def _on_toggle_excluded_at_frame(self, index: int) -> None:
        if self.cut_manager is not None:
            self._show_cut_context_menu(index, self.cursor().pos())

    def _show_cut_context_menu(self, index: int, global_position) -> None:
        if self.cut_manager is None or not (0 <= index < len(self.cut_manager.cuts)):
            return
        cut = self.cut_manager.cuts[index]
        menu = QMenu(self)
        action = menu.addAction(tr("cuts.unexclude") if cut.is_excluded else tr("cuts.exclude"))
        if menu.exec(global_position) is action:
            self.cut_manager.toggle_excluded(index)
            self._refresh_cut_list()

    # ---- ブックマーク (design doc §20) ----------------------------------------

    def _on_add_bookmark(self) -> None:
        if self.file_info is None:
            return
        frame = self.preview_widget.get_current_frame()
        if frame is None:
            return
        self.bookmark_manager.add_bookmark(frame)
        self._refresh_bookmark_list()

    def _on_remove_bookmark(self) -> None:
        indexes = self.bookmark_list.selectedIndexes()
        if not indexes:
            return
        self.bookmark_manager.remove_bookmark(indexes[0].row())
        self._refresh_bookmark_list()

    def _on_bookmark_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        index = self.bookmark_list.indexOfTopLevelItem(item)
        bookmark = self.bookmark_manager.get_bookmark(index)
        if bookmark is not None:
            self.preview_widget.seek_frame(bookmark.frame)

    def _refresh_bookmark_list(self) -> None:
        self.bookmark_list.clear()
        for bookmark in self.bookmark_manager.bookmarks:
            item = QTreeWidgetItem([str(bookmark.frame), bookmark.name])
            self.bookmark_list.addTopLevelItem(item)
        self.timeline.set_bookmarks(self.bookmark_manager.bookmarks)

    def _on_seek_slider_released(self) -> None:
        if self.file_info is not None:
            self.preview_widget.seek_frame(self.seek_slider.value())
        self._seek_slider_dragging = False

    # ---- 位置ポーリング (design doc §2.2, CLAUDE.md: UI更新はQtシグナル/タイマー経由) --

    def _poll_position(self) -> None:
        if self.file_info is None:
            return
        frame = self.preview_widget.get_current_frame()
        if frame is not None:
            self._update_time_label(frame)
            self.timeline.set_current_frame(frame)
            self.waveform_view.set_current_frame(frame)
            if not self._seek_slider_dragging:
                self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(frame)
                self.seek_slider.blockSignals(False)

    def _update_time_label(self, frame: int) -> None:
        if self.file_info is None:
            return
        text = frame_to_display(frame, self.file_info.total_frames, self.file_info.fps)
        self.time_label.setText(text)

    # ---- テーマ ---------------------------------------------------------

    def _on_os_color_scheme_changed(self, _scheme=None) -> None:
        if self.settings.theme == SYSTEM_THEME:
            self._set_theme(SYSTEM_THEME)

    def _set_theme(self, theme_name: str) -> None:
        from PySide6.QtWidgets import QApplication

        self.settings.theme = theme_name
        self.theme = ThemeManager.apply_theme(QApplication.instance(), theme_name)
        self.timeline.set_theme(self.theme)
        self.waveform_view.set_theme(self.theme)

    # ---- ダイアログ (design doc §22, §23, §25.1) -------------------------------

    def _on_open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self, self.preview_widget.get_audio_devices(),
                                history_db=self.history_db, shortcut_presets=self.shortcut_presets)
        if dialog.exec():
            self.shortcut_presets.replace_with(dialog.get_shortcut_presets())
            self._apply_shortcut_keys()
            # BatchManager/OutputManager/FFmpegWrapper等は起動時に受け取った
            # 同じAppSettingsオブジェクトを参照しているため、差し替えではなく
            # その場で値を更新する（差し替えると変更が再起動まで反映されない）。
            new_settings = dialog.get_settings()
            for field in dataclasses.fields(AppSettings):
                setattr(self.settings, field.name, getattr(new_settings, field.name))
            # 設定画面で変えたテーマもその場で反映する(以前は配色を読み直すだけで画面に
            # 適用しておらず、表示メニューで切り替えるか再起動するまで変わらなかった)
            self._set_theme(self.settings.theme)
            self.recent_files.set_max_count(self.settings.recent_files_count)
            self._apply_audio_device()
            self.waveform_action.setChecked(self.settings.show_waveform)
            self.waveform_view.setVisible(self.settings.show_waveform)
            self._update_disk_free()
            SettingsManager.save(self.settings)
            configure_logging(self.settings)
            logger.info("Settings changed and saved")

    def _on_show_shortcuts(self) -> None:
        ShortcutsDialog(self, self.shortcut_presets.active, self._preset_name(self.shortcut_presets.active_index)).exec()

    def _on_show_system_info(self) -> None:
        SystemInfoDialog(self.settings, self).exec()

    def _on_show_about(self) -> None:
        show_about_dialog(self, self.lang)

    # ---- バッチ処理 (design doc §8) -------------------------------------------

    def _on_add_to_batch(self) -> None:
        if self.file_info is None or self.cut_manager is None:
            QMessageBox.information(self, self.lang.get("menu.add_to_batch"), self.lang.get("messages.batch_no_file_open"))
            return
        if not self.cut_manager.cuts:
            QMessageBox.information(self, self.lang.get("menu.add_to_batch"), self.lang.get("messages.batch_no_keep_ranges"))
            return
        pending = len(self.cut_manager.pending_proposals)
        if pending:
            answer = QMessageBox.question(
                self, self.lang.get("menu.add_to_batch"),
                tr("main.pending_proposals_confirm", count=pending),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        # 保存名称を毎回尋ねる(2026-10-11 利用者の要望。1本の録画に入った複数の話を、名前を
        # 変えて話ごとに追加する。2つ目の話の区間は、1つ目を追加した後に手で消して付け直す)。
        # 初期値はパターンの名前(既存のファイル・バッチの他の項目と重なれば連番)
        source = self.file_info.file_path
        output_manager = self.batch_manager.output_manager
        others = self.batch_manager.output_paths()
        # 出力形式が AVS／VPY なら拡張子は .avs／.vpy(設計書 §6.9)
        extension = self.batch_manager.output_extension(source, self.settings.output_format)
        output_path = output_name_dialog.ask_output_name(
            self, output_manager.generate_output_path(source, reserved=others, extension=extension), source,
            lambda p: output_manager.check_output_path(p, source, others, extension),
            self.lang.get("menu.add_to_batch"),
        )
        if output_path is None:
            return
        self.file_info.cuts = list(self.cut_manager.cuts)
        # 追加した時点の区間・編集の履歴を渡す(追加の後の編集・「基準外」の切り替えは
        # バッチの項目に影響しない。エンコード開始と同じ)
        file_info = copy.deepcopy(self.file_info)
        file_info.cuts = copy.deepcopy(self.cut_manager.cuts)
        added = self.batch_list_view.add_item(
            file_info, processing_mode=self.settings.default_processing_mode,
            output_format=self.settings.output_format, program_info=self.program_info, edit_events=copy.deepcopy(self.cut_manager.edit_events),
            proposals=[p.to_dict() for p in self.cut_manager.proposals], output_path=output_path,
        )
        if not added:
            QMessageBox.information(self, self.lang.get("menu.add_to_batch"), self.lang.get("messages.batch_duplicate"))
        else:
            logger.info("Added to the batch list: %s", self.file_info.file_path)

    def _on_show_batch_tab(self) -> None:
        """バッチリストのタブを開く(隠していれば出す)。"""
        self.batch_tab_action.setChecked(True)
        self.lists_tabs.setCurrentWidget(self.batch_list_view)

    def _on_start_batch(self) -> None:
        logger.info("Started the batch (%d items)", len(self.batch_manager.items))
        if self.auto_tuner is not None:
            self.auto_tuner.cancel()  # 変換と重なると両方遅くなる。補正はバッチの後にやり直す
        self.batch_list_view.start()
        self._on_show_batch_tab()

    def _on_stop_batch(self) -> None:
        self.batch_list_view.stop()

    def _on_batch_finished(self) -> None:
        """バッチが終わったら、特徴を保存した録画の局と全局共通のしきい値を裏で補正する(設計書 §31.9)。"""
        recordings = self.feature_worker.take_saved_recordings()
        if self.auto_tuner is None or not recordings or self._closing:
            return
        try:
            stations = stations_of_recordings(self.history_db, recordings)
        except Exception:  # noqa: BLE001 - 補正できなくてもバッチの結果は変わらない
            logger.warning("Could not find the stations of the recordings to tune", exc_info=True)
            return
        self.auto_tuner.start(sorted(stations))

    def _on_thresholds_adopted(self, outcomes) -> None:
        """自動補正で採用した局をステータスバーに出す(次に開いた録画の案から使われる)。"""
        names = {}
        try:
            for s in self.history_db.list_stations():
                names[station_key(s["original_network_id"], s["service_id"])] = s["service_name"]
        except Exception:  # noqa: BLE001
            logger.debug("Could not read the station names", exc_info=True)
        scopes = [tr("messages.thresholds_all_stations") if not o.scope_key else (names.get(o.scope_key) or o.scope_key)
                  for o in outcomes]
        self.status_bar.showMessage(tr("messages.thresholds_tuned", stations=tr("messages.list_separator").join(scopes)),
                                    THRESHOLDS_MESSAGE_MS)

    def _on_open_logo_folder(self) -> None:
        """局ロゴ (.lgd) を置くフォルダを開く。置いた .lgd は録画を開く時に自動で取り込む (設計書 §31.9)。"""
        from ts_smartcut.autocut.lgd_import import logo_dir

        folder = logo_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create the logo folder: %s", folder, exc_info=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_stale_logo(self, request: int, stale) -> None:
        """取り込んだ局ロゴが古い時の知らせ。録画の読み込み中なら、読み込みが終わってから出す。"""
        if request != self._program_info_request:
            return
        self._pending_stale_logo = stale
        if not self._loading:
            self._show_stale_logo()

    def _show_stale_logo(self) -> None:
        from ts_smartcut.autocut.lgd_import import logo_dir

        stale, self._pending_stale_logo = self._pending_stale_logo, None
        if stale is None:
            return
        if self.history_db is not None:
            try:
                self.history_db.mark_imported_logo_stale_notified(stale.file_name)
            except Exception:  # noqa: BLE001 - 知らせは出す
                logger.warning("Could not record the stale logo notice: %s", stale.file_name, exc_info=True)
        QMessageBox.warning(self, tr("lgd_import.stale_title"), tr(
            "lgd_import.stale_message", file=stale.file_name, date=stale.logo_date or "",
            percent=round(stale.agreement * 100), folder=str(logo_dir())))

    def _on_logo_change(self, request: int, payload) -> None:
        """局のロゴが変わった可能性。録画の読み込み中なら、読み込みが終わってから聞く。"""
        if request != self._program_info_request:
            return
        self._pending_logo_change = payload
        if not self._loading:
            self._ask_logo_change()

    def _ask_logo_change(self) -> None:
        """「ロゴが変わった可能性があります。作り直しますか?」(設計書 §31.9)。はい: 合わなかった最初の
        録画から局の型を作り直す (古い型は期間付きで残す)。いいえ: それより後に 3 本たまるまで聞かない。"""
        from ts_smartcut.autocut.logo import restart_station_logo

        payload, self._pending_logo_change = self._pending_logo_change, None
        if payload is None or self.history_db is None:
            return
        change, station_name = payload
        answer = QMessageBox.question(self, tr("logo_change.title"), tr(
            "logo_change.question", station=station_name or str(change.service_id), count=len(change.agreements),
            agreements=" / ".join(f"{round(a * 100)}%" for a in change.agreements), since=change.since[:10]))
        try:
            if answer == QMessageBox.StandardButton.Yes:
                restart_station_logo(self.history_db, change.original_network_id, change.service_id, change.since)
                self.history_db.clear_logo_change_dismissal(change.original_network_id, change.service_id)
                logger.info("Restarted the station logo of service %s from %s", change.service_id, change.since)
                self.status_bar.showMessage(tr("logo_change.restarted"), STATUS_MESSAGE_MS)
            else:
                self.history_db.save_logo_change_dismissal(change.original_network_id, change.service_id,
                                                           change.since, change.latest)
                logger.info("Kept the station logo of service %s (asked again after %d newer recordings)",
                            change.service_id, len(change.agreements))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not update the station logo", exc_info=True)
            QMessageBox.warning(self, tr("logo_change.title"), tr("logo_change.failed", error=str(e)))

    def _open_history_db(self) -> Optional[CutHistoryDB]:
        try:
            return CutHistoryDB()
        except Exception as e:  # noqa: BLE001 - 自動カットの失敗でアプリを止めない
            error = e if isinstance(e, TSSmartCutError) else TSSmartCutError("E999", str(e))
            logger.error("[%s] Cannot open the automatic cut history DB: %s", error.code, error.log_message, exc_info=True)
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, self.lang.get("messages.history_db_title"),
                self.lang.get("messages.history_db_error", code=error.code, message=error.message),
            ))
            return None

    # ---- エンコード開始ボタン(バッチリストを使わず今の残す区間で処理する) --------

    def is_encoding(self) -> bool:
        return self.encode_manager is not None and self.encode_manager.is_running

    def _on_start_encode(self) -> None:
        title = self.lang.get("messages.encode_title")
        if self.file_info is None or self.cut_manager is None:
            QMessageBox.information(self, title, self.lang.get("messages.batch_no_file_open"))
            return
        if not self.cut_manager.cuts:
            QMessageBox.information(self, title, self.lang.get("messages.batch_no_keep_ranges"))
            return
        if self.batch_manager.is_running:
            QMessageBox.information(self, title, self.lang.get("messages.encode_batch_running"))
            return
        # 押した時点の区間で処理する(処理中に画面の状態が変わっても影響しない)
        file_info = copy.deepcopy(self.file_info)
        file_info.cuts = copy.deepcopy(self.cut_manager.cuts)
        # 記録しない(特徴も計算しない)
        self.encode_manager = BatchManager(self.settings, hardware=self._encode_hardware, record_autocut=False)
        self.encode_manager.add_item(file_info, processing_mode=self.settings.default_processing_mode,
                                     output_format=self.settings.output_format)

        # 再生中なら止める(プレビューは処理中の位置を表示する)
        try:
            self.preview_widget.command(["set", "pause", "yes"])
        except TSSmartCutError:
            pass
        self._set_encoding_ui(True)
        self._on_encode_progress(None)
        logger.info("Started encoding: %s", file_info.file_path)
        self.encode_manager.start(progress_callback=self._encode_bridge.progress.emit)
        self._encode_timer = QTimer(self)
        self._encode_timer.setInterval(200)
        self._encode_timer.timeout.connect(self._check_encode_finished)
        self._encode_timer.start()

    # テストではハードウェア判定(実際にGPUで試しエンコードする)を差し替える
    _encode_hardware = None

    def _on_cancel_encode(self) -> None:
        if self.is_encoding():
            self.cancel_encode_button.setEnabled(False)
            self.encode_manager.cancel()

    def _on_encode_progress(self, progress) -> None:
        """エンコードの進み具合をステータスバーに出し、処理中の位置へプレビューと
        シークバーを動かす(GUIのスレッドで呼ばれる)。"""
        if progress is None:
            # 最初の段階が始まるまで(キーフレームや元フレームの下調べで数秒かかる)
            self.encode_progress_bar.setValue(0)
            self.encode_label.setText(self.lang.get("messages.encode_preparing"))
            return
        self.encode_progress_bar.setValue(int(progress.progress_percent))
        self.encode_label.setText(self.lang.get(
            "messages.encode_progress",
            phase=progress.current_phase,
            step=progress.current_step + 1,
            steps=progress.total_steps,
            percent=int(progress.progress_percent),
        ))
        frame = progress.source_frame
        if frame is None or frame == self._encode_last_frame or self.file_info is None:
            return
        self._encode_last_frame = frame
        try:
            self.preview_widget.seek_frame(frame)
        except TSSmartCutError as e:
            logger.debug("Could not move the preview during encoding: %s", e.log_message)
        # mpvが無い場合でもシークバーと時刻は動かす(mpvがあれば位置の
        # ポーリングでも同じ値になる)
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(frame)
        self.seek_slider.blockSignals(False)
        self._update_time_label(frame)
        self.timeline.set_current_frame(frame)
        self.waveform_view.set_current_frame(frame)

    def _check_encode_finished(self) -> None:
        if self.is_encoding():
            return
        # 処理のスレッドから届いて溜まっている進捗を先に処理する。Qt はタイマーと
        # スレッドからのシグナルの順を決めないので、これが無いと最後の進捗が操作を
        # 戻した後に届き、プレビューを動かすことがあった(CI の Linux で見つけた、2026-11-16)。
        QApplication.sendPostedEvents(None, QEvent.Type.MetaCall)
        self._encode_timer.stop()
        self._set_encoding_ui(False)
        item = self.encode_manager.items[0]
        title = self.lang.get("messages.encode_title")
        if item.status == STATUS_DONE:
            logger.info("Encoding finished: %s", item.output_path)
            self._update_disk_free()
            QMessageBox.information(self, title, self.lang.get("messages.encode_done", path=str(item.output_path)))
        elif item.error_code == "E504":
            QMessageBox.information(self, title, self.lang.get("messages.encode_cancelled"))
        else:
            QMessageBox.warning(self, title, self.lang.get(
                "messages.encode_failed", code=item.error_code, message=(item.error_message or "")[-1500:],
            ))

    def _set_encoding_ui(self, encoding: bool) -> None:
        """エンコード中は画面の操作を受け付けない(利用者の要件 2026-10-06)。
        メニュー・画面の部品・ショートカット・バッチリストの窓を止め、ステータス
        バーの中止ボタンだけ押せるようにする。プレビューとシークバーは処理に
        合わせてこちらから動かす。"""
        self._set_ui_enabled(not encoding and not self._loading)
        for widget in (self.encode_label, self.encode_progress_bar, self.cancel_encode_button):
            widget.setVisible(encoding)
        self.cancel_encode_button.setEnabled(encoding)
        self._encode_last_frame = None
        if encoding:
            self.status_bar.clearMessage()

    # ---- キーボードショートカット (design doc §4.5) -----------------------------

    def _build_shortcuts(self) -> None:
        """操作ごとに QShortcut を 1 つ作る(キーは使っているキープリセットから。設計書 §4.5.1)。
        割り当てなしの操作も作っておく(プリセットを切り替えた時にキーを入れるだけで済む)。"""
        handlers = {
            "play_pause": self._on_toggle_pause,
            "frame_back": self._on_frame_back,
            "frame_forward": self._on_frame_forward,
            "prev_keyframe": self._on_prev_keyframe,
            "next_keyframe": self._on_next_keyframe,
            "speed_down": lambda: self._on_speed_step(-1),
            "speed_up": lambda: self._on_speed_step(1),
            "set_start": self._on_set_start,
            "set_end": self._on_set_end,
            "jump_start": self._on_jump_to_selected_start,
            "jump_end": self._on_jump_to_selected_end,
            "clear_selection": self._on_clear_cut_selection,
            "add_cut": self._on_add_cut,
            "add_bookmark": self._on_add_bookmark,
            "delete_cut": self._on_delete_cut,
            "accept_proposal": lambda: self._on_accept_proposal(),
            "reject_proposal": lambda: self._on_reject_proposal(),
            "mute": self._on_toggle_mute_key,
            "undo": self._on_undo,
            "redo": self._on_redo,
            "save_project": self._on_save_project,
            "open": self._on_open,
            "help": self._on_show_shortcuts,
            "exit": self.close,
        }
        self._shortcuts = []
        self._shortcut_by_action = {}
        for action, handler in handlers.items():
            shortcut = QShortcut(self)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)
            self._shortcut_by_action[action] = shortcut
        self._apply_shortcut_keys()

    def _apply_shortcut_keys(self) -> None:
        """使っているプリセットのキーを QShortcut と画面の表示(メニュー・ボタン)に入れる。"""
        for action, shortcut in self._shortcut_by_action.items():
            key = self.shortcut_presets.key_for(action)
            shortcut.setKey(QKeySequence(key, QKeySequence.SequenceFormat.PortableText) if key else QKeySequence())
        self._refresh_key_labels()
        logger.info("Shortcut keys set from preset %d", self.shortcut_presets.active_index + 1)

    def _key_text(self, action: str) -> str:
        return display_key(self.shortcut_presets.key_for(action))

    def _with_key(self, text: str, action: str) -> str:
        """「Start設定 (S)」のように、割り当てたキーを後ろに付ける(割り当てなしなら付けない)。"""
        key = self._key_text(action)
        return f"{text} ({key})" if key else text

    def _refresh_key_labels(self) -> None:
        t = self.lang.get
        # 開く・終了は、前に setShortcut で付いていた表示(メニューの右側)に合わせる
        for menu_action, label, action in ((self.open_action, "menu.open", "open"),
                                           (self.exit_action, "menu.exit", "exit")):
            key = self._key_text(action)
            menu_action.setText(f"{t(label)}\t{key}" if key else t(label))
        for widget, label, action in (
            (self.save_project_action, "menu.save_project", "save_project"),
            (self.undo_action, "menu.undo", "undo"),
            (self.redo_action, "menu.redo", "redo"),
            (self.set_start_action, "menu.set_start", "set_start"),
            (self.set_end_action, "menu.set_end", "set_end"),
            (self.add_cut_action, "menu.add_cut", "add_cut"),
            (self.delete_cut_action, "menu.delete_cut", "delete_cut"),
            (self.add_bookmark_action, "menu.add_bookmark", "add_bookmark"),
            (self.shortcuts_action, "menu.shortcuts", "help"),
            (self.set_start_button, "buttons.set_start", "set_start"),
            (self.set_end_button, "buttons.set_end", "set_end"),
            (self.add_cut_button, "buttons.add_cut", "add_cut"),
            (self.delete_cut_button, "buttons.delete_cut", "delete_cut"),
            (self.jump_start_button, "buttons.jump_start", "jump_start"),
            (self.jump_end_button, "buttons.jump_end", "jump_end"),
        ):
            widget.setText(self._with_key(t(label), action))
        self.accept_proposal_button.setToolTip(self._with_key(tr("proposals.accept_tip"), "accept_proposal"))
        self.reject_proposal_button.setToolTip(self._with_key(tr("proposals.reject_tip"), "reject_proposal"))
        self._update_mute_button()
        self._update_pending_label()

    def _rebuild_shortcut_preset_menu(self) -> None:
        menu = self.shortcut_preset_menu
        menu.clear()
        for index in range(PRESET_COUNT):
            action = QAction(self._preset_name(index), menu)
            action.setCheckable(True)
            action.setChecked(index == self.shortcut_presets.active_index)
            action.triggered.connect(lambda _checked=False, i=index: self._on_choose_shortcut_preset(i))
            menu.addAction(action)

    def _preset_name(self, index: int) -> str:
        return self.shortcut_presets.presets[index].name or tr("shortcuts.preset_default_name", n=index + 1)

    def _on_choose_shortcut_preset(self, index: int) -> None:
        self.shortcut_presets.set_active(index)
        self._apply_shortcut_keys()
        self._rebuild_shortcut_preset_menu()

    def wheelEvent(self, event) -> None:
        """design doc §4.5: マウスホイールで1フレーム移動、Ctrl+ホイールで音量調整。"""
        if self._ui_locked():
            event.accept()
            return
        if self.file_info is None:
            super().wheelEvent(event)
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._adjust_volume(5 if event.angleDelta().y() > 0 else -5)
        elif event.angleDelta().y() > 0:
            self._on_frame_back()
        else:
            self._on_frame_forward()
        event.accept()

    def _adjust_volume(self, delta: int) -> None:
        self.settings.playback_volume = max(0, min(100, self.settings.playback_volume + delta))
        self.preview_widget.set_volume(self.settings.playback_volume)

    # ---- 終了 -----------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.is_encoding():
            # エンコードが終わるまで閉じない(やめるには中止ボタン)
            event.ignore()
            QMessageBox.information(self, self.lang.get("messages.encode_title"), self.lang.get("messages.encode_close_blocked"))
            return
        if self.feature_worker.is_busy:
            # 特徴の計算中は閉じない(利用者の指示 2026-10-08。閉じると続きを計算できない)
            event.ignore()
            QMessageBox.information(
                self, self.lang.get("messages.history_db_title"),
                self.lang.get("messages.features_close_blocked", count=self.feature_worker.pending_count),
            )
            return
        # 読み込み中も閉じられる(読み込みと案の準備をやめて閉じる、§4.11)
        self._closing = True
        if self._proposal_stop is not None:
            self._proposal_stop.set()
        self._end_loading()
        self._poll_timer.stop()
        if self.batch_manager.is_running:
            self.batch_manager.stop()
        # バッチを止めた後に計算を始めないようにする
        self.feature_worker.stop()
        if self.auto_tuner is not None:
            self.auto_tuner.cancel(wait=5.0)  # 補正は途中でやめてよい(DB は採用した時にだけ書く)
        if self._proposal_stop is not None:
            self._proposal_stop.set()
        self.preview_widget.cleanup()
        super().closeEvent(event)
