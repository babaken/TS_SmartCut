"""バッチリストUI (design doc §4.1, §8)。

BatchManagerは自前のバックグラウンドスレッドで動く(design doc §8.1)。
進捗コールバックはそのスレッドから呼ばれるため、mpvのupdate_cbと同じ
パターンでQtのシグナル(キュー接続)経由でGUIスレッドへブリッジする。
各アイテムのステータス変化(Idle/Processing/Done/Error/Skipped)は
コールバックを持たないため、定期ポーリングでリスト表示を更新する
（design doc §29: すべての操作はスレッド安全、UI更新はQtのシグナル/
タイマー経由）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ts_smartcut.broadcast.audio_tracks import AudioTrack
from ts_smartcut.core.batch_manager import (
    PHASE_FEATURES,
    PHASE_SCRIPT,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_SKIPPED,
    BatchManager,
)
from ts_smartcut.gui import output_name_dialog
from ts_smartcut.core import script_output
from ts_smartcut.gui.settings_dialog import OUTPUT_FORMAT_LABELS, PROCESSING_MODE_LABELS
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.ffmpeg.smart_cut import PROCESSING_MODES, resolve_processing_mode
from ts_smartcut.models import BatchItem, FileCutInfo, ProcessProgress

logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL_MS = 300

COLUMN_ORDER, COLUMN_FILE, COLUMN_OUTPUT, COLUMN_DURATION, COLUMN_MODE, COLUMN_STATUS, COLUMN_ERROR = range(7)
# 列の幅の初期値(どの列もドラッグで変えられ、幅は設定に覚える。最後の「エラー」は残りの幅)
DEFAULT_COLUMN_WIDTHS = [50, 330, 280, 80, 130, 110, 220]



def processing_mode_label(item: BatchItem) -> str:
    """モード欄の表示。VFRでSmart CutがSimple Copyに切り替わる項目はそれと分かるようにする
    (design doc §16.3)。出力形式が AVS／VPY の項目は処理モードを使わないので形式を出す(§6.9)。"""
    if script_output.is_script_format(item.output_format):
        return OUTPUT_FORMAT_LABELS[item.output_format]
    mode, _ = resolve_processing_mode(item.processing_mode, item.file_info.is_vfr)
    label = PROCESSING_MODE_LABELS.get(mode, mode)
    return tr("batch.vfr_mode", label=label) if mode != item.processing_mode else label

def output_duration_text(file_info: FileCutInfo) -> str:
    """カット後の再生時間(残す区間の合計、秒まで・切り捨て。2026-10-17 利用者の要望)。残す区間の一覧の
    見出しの確定再生時間と同じ計算。Simple Copy の出力はキーフレームに合わせて少し短くなることがあるが、
    編集で決めた長さを出す(利用者の決定)。"""
    if file_info.fps <= 0:
        return ""
    seconds = int(sum(cut.end_frame - cut.start_frame for cut in file_info.cuts) / file_info.fps)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _hms(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class _ProgressBridge(QObject):
    progress = Signal(object)


class _ReorderableTreeWidget(QTreeWidget):
    """design doc §4.1: バッチリストはドラッグで並び替え可能。
    ドロップ後の移動元/移動先インデックスをitem_movedで通知する。"""

    item_moved = Signal(int, int)

    def dropEvent(self, event) -> None:
        dragged_item = self.currentItem()
        old_index = self.indexOfTopLevelItem(dragged_item) if dragged_item is not None else -1
        super().dropEvent(event)
        if dragged_item is not None:
            new_index = self.indexOfTopLevelItem(dragged_item)
            if old_index >= 0 and new_index >= 0 and old_index != new_index:
                self.item_moved.emit(old_index, new_index)


class BatchListView(QWidget):
    # タブの名前(件数・実行中は「終わった数/全部」)が変わった
    title_changed = Signal(str)
    # 列の幅をドラッグで変えた(設定に覚える)
    column_widths_changed = Signal(list)
    # バッチが終わった(止めた時も。しきい値の自動補正を始める)
    batch_finished = Signal()

    def __init__(self, batch_manager: BatchManager, parent=None):
        super().__init__(parent)
        self.batch_manager = batch_manager
        # 実行中だったか(終わった時に一度だけ結果を出す)と、最後に知らされた経過秒
        self._was_running = False
        self._last_elapsed = None
        self._bridge = _ProgressBridge()
        self._bridge.progress.connect(self._on_progress)

        layout = QVBoxLayout(self)

        self.tree = _ReorderableTreeWidget()
        # 保存名称は出力ファイルの名前(バッチ追加の時に決める。右クリックの「名称変更」で変えられる)
        self.tree.setHeaderLabels([tr(f"batch.column_{name}")
                                   for name in ("order", "file", "output", "duration", "mode", "status", "error")])
        # どの列も境目のドラッグで幅を変えられる(2026-10-11 利用者の要望。前はファイル名の
        # 列が残りの幅を使い、他は中身に合わせた幅で変えられなかった)。最後の列は残りの幅。
        # ファイル名の全文はツールチップで見られる
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.set_column_widths(DEFAULT_COLUMN_WIDTHS)
        header.sectionResized.connect(self._on_section_resized)
        self._title = ""
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.item_moved.connect(self._on_item_moved)
        # 右クリックで項目ごとに処理モードを変える (design doc §6.1)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.tree)

        controls = QHBoxLayout()
        self.remove_button = QPushButton(tr("buttons.batch_remove"))
        self.remove_button.clicked.connect(self._on_remove_selected)
        controls.addWidget(self.remove_button)

        self.retry_button = QPushButton(tr("buttons.batch_retry"))
        self.retry_button.clicked.connect(self._on_retry_selected)
        controls.addWidget(self.retry_button)

        self.start_button = QPushButton(tr("buttons.batch_start"))
        self.start_button.clicked.connect(self.start)
        controls.addWidget(self.start_button)

        self.stop_button = QPushButton(tr("buttons.batch_stop"))
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.stop_button)

        self.skip_button = QPushButton(tr("buttons.batch_skip"))
        self.skip_button.clicked.connect(self.skip_current)
        controls.addWidget(self.skip_button)

        # 完了した項目だけを一覧から消す(実行中・完了が無い時は押せない。2026-10-11)
        self.clear_done_button = QPushButton(tr("batch.clear_done"))
        self.clear_done_button.setToolTip(tr("batch.clear_done_tip"))
        self.clear_done_button.setEnabled(False)
        self.clear_done_button.clicked.connect(self.clear_done)
        controls.addWidget(self.clear_done_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        self.progress_label = QLabel("")
        layout.addWidget(self.progress_label)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_POLL_INTERVAL_MS)
        self._status_timer.timeout.connect(self._refresh_list)
        self._status_timer.start()

    def add_item(self, file_info: FileCutInfo, processing_mode: str = "smart_cut",
                 program_info=None, edit_events=None, proposals=None, output_path=None,
                 output_format: str = script_output.OUTPUT_TS) -> bool:
        added = self.batch_manager.add_item(
            file_info, processing_mode=processing_mode, output_path=output_path, program_info=program_info,
            edit_events=edit_events, proposals=proposals, output_format=output_format,
        )
        if added:
            self._refresh_list()
        return added

    def start(self) -> bool:
        started = self.batch_manager.start(progress_callback=self._on_progress_from_worker_thread)
        if started:
            # 0.3秒の見回りより先に終わる短いバッチでも、終わった結果を出す
            self._was_running = True
            self._last_elapsed = None
        return started

    def stop(self) -> None:
        self.batch_manager.stop()

    def skip_current(self) -> None:
        self.batch_manager.skip_current()

    def clear_done(self) -> int:
        removed = self.batch_manager.remove_completed()
        if removed:
            self._refresh_list()
        return removed

    def title(self) -> str:
        """タブの名前。項目が無ければ「バッチリスト」、あれば件数、実行中は終わった数/全部。"""
        items = self.batch_manager.items
        if not items:
            return tr("batch.tab")
        if self.batch_manager.is_running:
            finished = sum(1 for item in items if item.status in (STATUS_DONE, STATUS_ERROR, STATUS_SKIPPED))
            return tr("batch.tab_progress", finished=finished, total=len(items))
        return tr("batch.tab_count", count=len(items))

    def column_widths(self) -> list:
        header = self.tree.header()
        return [header.sectionSize(column) for column in range(header.count())]

    def set_column_widths(self, widths) -> None:
        """最後の列は残りの幅なので、それより前の列だけ合わせる。数の合わない値は使わない。
        再生時間の列が無かった頃の 6 列の値は、その列に初期値を入れて使う(2026-10-17)。"""
        header = self.tree.header()
        if widths and len(widths) == header.count() - 1:
            widths = list(widths[:COLUMN_DURATION]) + [DEFAULT_COLUMN_WIDTHS[COLUMN_DURATION]] + list(widths[COLUMN_DURATION:])
        if not widths or len(widths) != header.count():
            return
        for column, width in enumerate(widths[:-1]):
            if isinstance(width, int) and width > 0:
                header.resizeSection(column, width)

    def _on_section_resized(self, _column: int, _old: int, _new: int) -> None:
        self.column_widths_changed.emit(self.column_widths())

    def _on_item_moved(self, old_index: int, new_index: int) -> None:
        # 実行中はBatchManager.reorder()がFalseを返す(design doc §8.1)。
        # その場合も_refresh_list()でBatchManagerの実際の順序に描画を
        # 揃え直し、ドラッグ操作の見た目だけが先行した状態を解消する。
        self.batch_manager.reorder(old_index, new_index)
        self._refresh_list()

    def _on_remove_selected(self) -> None:
        indexes = self.tree.selectedIndexes()
        if not indexes:
            return
        self.batch_manager.remove_item(indexes[0].row())
        self._refresh_list()

    def _on_retry_selected(self) -> None:
        indexes = self.tree.selectedIndexes()
        if not indexes:
            return
        self.batch_manager.retry_item(indexes[0].row())
        self._refresh_list()

    def mode_menu(self, row: int) -> QMenu:
        """row の項目の処理モードを選ぶメニュー。未処理・エラーの項目以外と、
        実行中は選べない。出力形式が AVS／VPY の項目は処理モードを使わないので選べない。"""
        menu = QMenu(self)
        item = self.batch_manager.items[row]
        editable = not self.batch_manager.is_running and item.status in (STATUS_IDLE, STATUS_ERROR)
        uses_mode = not script_output.is_script_format(item.output_format)
        for mode in PROCESSING_MODES:
            action = menu.addAction(PROCESSING_MODE_LABELS[mode])
            action.setCheckable(True)
            action.setChecked(uses_mode and mode == item.processing_mode)
            action.setEnabled(editable and uses_mode)
            action.triggered.connect(lambda _checked=False, m=mode: self.set_mode(row, m))
        return menu

    def output_format_menu(self, row: int, parent: QMenu) -> QMenu:
        """row の項目の出力形式(TS／AVS／VPY、設計書 §6.9)を選ぶメニュー。この項目だけを変える(一括では
        変えない)。処理モードと同じく未処理・エラーの項目だけ、実行中は選べない。"""
        menu = parent.addMenu(tr("batch.output_format_menu"))
        item = self.batch_manager.items[row]
        editable = not self.batch_manager.is_running and item.status in (STATUS_IDLE, STATUS_ERROR)
        for output_format, label in OUTPUT_FORMAT_LABELS.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(output_format == item.output_format)
            action.setEnabled(editable)
            action.triggered.connect(lambda _checked=False, f=output_format: self.set_output_format(row, f))
        return menu

    def set_output_format(self, row: int, output_format: str) -> bool:
        if not self.batch_manager.set_output_format(row, output_format):
            return False
        logger.info("Changed the output format of batch item %d to %s", row + 1, output_format)
        self._refresh_list()
        return True

    def set_mode(self, row: int, mode: str) -> None:
        if self.batch_manager.set_processing_mode(row, mode):
            self._refresh_list()

    def item_menu(self, row: int) -> QMenu:
        """右クリックのメニュー: 処理モード・「出力形式」・「名称変更」(未処理・エラー・スキップの項目だけ)と、
        音声が 2 つ以上ある録画は「音声」(出力に入れる音声、設計書 §4.12。処理モードと同じく未処理・エラーの
        項目だけ)。"""
        menu = self.mode_menu(row)
        menu.addSeparator()
        self.output_format_menu(row, menu)
        action = menu.addAction(tr("batch.rename"))
        action.setEnabled(self.batch_manager.can_rename(row))
        action.triggered.connect(lambda _checked=False: self.rename(row))
        item = self.batch_manager.items[row]
        tracks = [AudioTrack.from_dict(t) for t in item.file_info.audio_tracks]
        if len(tracks) >= 2:
            audio_menu = menu.addMenu(tr("batch.audio_menu"))
            editable = not self.batch_manager.is_running and item.status in (STATUS_IDLE, STATUS_ERROR)
            chosen = item.file_info.selected_audio_index()
            for track in tracks:
                audio_action = audio_menu.addAction(track.label())
                audio_action.setCheckable(True)
                audio_action.setChecked(track.index == chosen)
                audio_action.setEnabled(editable)
                audio_action.triggered.connect(lambda _checked=False, t=track: self.set_audio(row, t))
        return menu

    def set_audio(self, row: int, track: AudioTrack) -> bool:
        if not self.batch_manager.set_audio_track(row, track.to_dict()):
            return False
        logger.info("Changed the audio of batch item %d to track %d (PID 0x%X)", row + 1, track.index + 1, track.pid)
        self._refresh_list()
        return True

    def rename(self, row: int) -> bool:
        """項目の保存名称を尋ねて変える。既存のファイル・他の項目と同じ名前は注意を出して直してもらう。"""
        if not self.batch_manager.can_rename(row):
            return False
        item = self.batch_manager.items[row]
        source = item.file_info.file_path
        output_manager = self.batch_manager.output_manager
        others = self.batch_manager.output_paths(exclude_index=row)
        extension = self.batch_manager.output_extension(source, item.output_format)
        initial = item.output_path or output_manager.generate_output_path(source, reserved=others, extension=extension)
        path = output_name_dialog.ask_output_name(
            self, initial, source, lambda p: output_manager.check_output_path(p, source, others, extension),
            tr("batch.rename_title"))
        if path is None or not self.batch_manager.rename_item(row, path):
            return False
        self._refresh_list()
        return True

    def _on_context_menu(self, pos) -> None:
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        self.item_menu(index.row()).exec(self.tree.viewport().mapToGlobal(pos))

    def _on_progress_from_worker_thread(self, progress: ProcessProgress) -> None:
        # BatchManagerのワーカースレッドから呼ばれる。GUIを直接触らず、
        # シグナル(キュー接続)経由でメインスレッドへ渡す。
        self._bridge.progress.emit(progress)

    def _on_progress(self, progress: ProcessProgress) -> None:
        self.progress_bar.setValue(int(progress.progress_percent))
        # 特徴の計算(変換の後、Done の前)。終わるまで録画を移動しない
        phase = {PHASE_FEATURES: tr("batch.phase_features"),
                 PHASE_SCRIPT: tr("batch.phase_script")}.get(progress.current_phase, progress.current_phase)
        # 経過時間だけ出す(残り時間は誤差が大きいので出さない、§8.4・§29。2026-10-09 利用者の決定)
        elapsed = int(progress.elapsed_time)
        self._last_elapsed = elapsed
        self.progress_label.setText(
            f"{progress.current_file}/{progress.total_files}  "
            f"{phase}  {progress.progress_percent:.0f}%  " + tr("batch.elapsed", time=_hms(elapsed))
        )

    def _refresh_list(self) -> None:
        """0.3秒ごとに呼ばれる。行の数が同じなら文字だけ書き換える(全部を作り直すと
        スクロール位置が毎回先頭に戻り、長いリストを下まで見られなかった。2026-10-10)。"""
        rows = []
        for item in self.batch_manager.items:
            error_text = f"[{item.error_code}] {item.error_message}" if item.error_code else ""
            output = item.output_path
            texts = [str(item.order), item.file_info.file_path.name, output.name if output else "",
                     output_duration_text(item.file_info), processing_mode_label(item), item.status, error_text]
            rows.append((texts, str(item.file_info.file_path), str(output) if output else "", error_text))
        if self.tree.topLevelItemCount() == len(rows):
            for index, (texts, path_text, output_text, error_text) in enumerate(rows):
                tree_item = self.tree.topLevelItem(index)
                for column, text in enumerate(texts):
                    if tree_item.text(column) != text:
                        tree_item.setText(column, text)
                tree_item.setToolTip(COLUMN_FILE, path_text)
                tree_item.setToolTip(COLUMN_OUTPUT, output_text)
                tree_item.setToolTip(COLUMN_ERROR, error_text)
        else:
            selected_row = self.tree.currentIndex().row()
            scroll = self.tree.verticalScrollBar().value()
            self.tree.clear()
            for texts, path_text, output_text, error_text in rows:
                tree_item = QTreeWidgetItem(texts)
                tree_item.setToolTip(COLUMN_FILE, path_text)
                tree_item.setToolTip(COLUMN_OUTPUT, output_text)
                tree_item.setToolTip(COLUMN_ERROR, error_text)
                self.tree.addTopLevelItem(tree_item)
            if 0 <= selected_row < self.tree.topLevelItemCount():
                self.tree.setCurrentItem(self.tree.topLevelItem(selected_row))
            self.tree.verticalScrollBar().setValue(scroll)
        self.clear_done_button.setEnabled(
            not self.batch_manager.is_running
            and any(item.status == STATUS_DONE for item in self.batch_manager.items))
        self._show_finished_state()
        title = self.title()
        if title != self._title:
            self._title = title
            self.title_changed.emit(title)

    def _show_finished_state(self) -> None:
        """バッチが終わったら、進み具合の欄を結果に置き換える(最後の項目の特徴の計算の
        「82%」などが残り、終わったのか分からなかった。2026-10-10)。"""
        running = self.batch_manager.is_running
        if running:
            self._was_running = True
            return
        if not self._was_running:
            return
        self._was_running = False
        items = self.batch_manager.items
        counts = {status: sum(1 for item in items if item.status == status)
                  for status in (STATUS_DONE, STATUS_ERROR, STATUS_SKIPPED, STATUS_IDLE)}
        if counts[STATUS_IDLE] == 0:
            self.progress_bar.setValue(100)
        summary = tr("batch.summary", done=counts[STATUS_DONE], error=counts[STATUS_ERROR],
                     skipped=counts[STATUS_SKIPPED])
        if counts[STATUS_IDLE]:
            summary += tr("batch.summary_idle", idle=counts[STATUS_IDLE])
        if self._last_elapsed is not None:
            summary += "  " + tr("batch.elapsed", time=_hms(self._last_elapsed))
        self.progress_label.setText(summary)
        self.batch_finished.emit()
