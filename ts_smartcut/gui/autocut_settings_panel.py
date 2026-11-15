"""設定の「自動カット」タブ (設計書 §31.9、自動カット計画 A9)。

中を 3 つのタブに分ける (1080p・150% の画面に収まるように):
- 全般: 自動カットの案を出す・ロゴの変化を知らせる (入／切。設定の OK で保存)、記録の本数・記録 DB の
  場所・「フォルダを開く」。
- 局とロゴ: 局ごとの表 (局名・録画の本数・局の型・型の期間・取り込んだ局ロゴ) と「ロゴを作り直す」・
  「ロゴを書き出す (.lgd)」。作り直しは確かめてからその場で行う (設定の OK を待たない。記録 DB の操作で、
  取り消しはできない)。書き出しは選んだフォルダに .lgd を書く (lgd_export.py、既にあるファイルは上書きしない)。
- しきい値: threshold_panel.ThresholdPanel。
- 学習 (A8、2026-10-26): 既定の方式 (ルール／学習。設定の OK で保存)、学習の状態、「学習し直す」(裏のスレッド、
  中止できる。結果をルールと比べて出す) と「学習をクリア」(どちらも確かめてからその場で行う)。
  局ごとの方式と一致率 (確認用の録画での再現率) は「局とロゴ」の表の列。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ts_smartcut.autocut import model as learning
from ts_smartcut.autocut.thresholds import station_key
from ts_smartcut.gui.threshold_panel import ThresholdPanel
from ts_smartcut.i18n.language_manager import tr

logger = logging.getLogger(__name__)

(COL_STATION, COL_RECORDINGS, COL_TEMPLATE, COL_PERIOD, COL_IMPORTED,
 COL_METHOD, COL_ACCURACY) = range(7)
_METHODS = (learning.METHOD_DEFAULT, learning.METHOD_RULE, learning.METHOD_LEARNED)


class _RetrainBridge(QObject):
    """学習のスレッドから画面へ(キューの接続)。"""

    stage = Signal(str, int, int)
    finished = Signal(object)  # TrainingResult
    failed = Signal(str)  # 空 = 中止


def _date(value: Optional[str]) -> str:
    return (value or "")[:10]


class AutoCutSettingsPanel(QWidget):
    def __init__(self, settings, history_db, parent=None):
        super().__init__(parent)
        self._db = history_db
        self._stations = []  # 表の行ごとの局 (list_stations の行)
        self._station_methods: Dict[str, str] = dict(settings.autocut_station_methods)
        self._method_combos = []  # 表の行ごとの (局の鍵, 方式の選択)
        self._retrain_stop: Optional[threading.Event] = None
        self._retrain_dialog: Optional[QProgressDialog] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.tabs.addTab(self._build_general(settings), tr("autocut_settings.tab_general"))
        self.tabs.addTab(self._build_stations(), tr("autocut_settings.tab_stations"))
        self.threshold_panel = ThresholdPanel(history_db)
        self.thresholds_tab_index = self.tabs.addTab(self.threshold_panel, tr("autocut_settings.tab_thresholds"))
        self.tabs.addTab(self._build_learning(settings), tr("autocut_settings.tab_learning"))
        self.refresh_learning()
        self.refresh_stations()

    # ---- 全般 --------------------------------------------------------------

    def _build_general(self, settings) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        proposals = QGroupBox(tr("autocut_settings.group_proposals"))
        form = QVBoxLayout(proposals)
        self.proposals_check = QCheckBox(tr("autocut_settings.proposals"))
        self.proposals_check.setChecked(settings.autocut_proposals)
        form.addWidget(self.proposals_check)
        self.logo_change_check = QCheckBox(tr("autocut_settings.notify_logo_change"))
        self.logo_change_check.setChecked(settings.notify_logo_change)
        form.addWidget(self.logo_change_check)
        hint = QLabel(tr("autocut_settings.notify_logo_change_hint"))
        hint.setWordWrap(True)
        form.addWidget(hint)
        layout.addWidget(proposals)

        record = QGroupBox(tr("autocut_settings.group_record"))
        record_form = QFormLayout(record)
        self.record_count_label = QLabel()
        self.db_path_label = QLabel()
        self.db_path_label.setWordWrap(True)
        record_form.addRow(tr("autocut_settings.record_count"), self.record_count_label)
        record_form.addRow(tr("autocut_settings.db_path"), self.db_path_label)
        self.open_folder_button = QPushButton(tr("autocut_settings.open_folder"))
        self.open_folder_button.clicked.connect(self._on_open_folder)
        record_form.addRow(self.open_folder_button)
        layout.addWidget(record)
        layout.addStretch(1)

        if self._db is None:
            self.record_count_label.setText(tr("autocut_settings.no_db"))
            self.db_path_label.setText("")
            self.open_folder_button.setEnabled(False)
        else:
            self.db_path_label.setText(str(self._db.db_path))
            try:
                self.record_count_label.setText(tr("autocut_settings.record_count_value",
                                                   count=self._db.count_recordings()))
            except Exception:  # noqa: BLE001 - 表示だけ
                logger.warning("Could not count the recordings in the history DB", exc_info=True)
        return widget

    def _on_open_folder(self) -> None:
        if self._db is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._db.db_path.parent)))

    # ---- 局とロゴ --------------------------------------------------------------

    def _build_stations(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.station_table = QTableWidget(0, 7)
        self.station_table.setHorizontalHeaderLabels([
            tr("autocut_settings.col_station"), tr("autocut_settings.col_recordings"),
            tr("autocut_settings.col_template"), tr("autocut_settings.col_period"),
            tr("autocut_settings.col_imported"), tr("autocut_settings.col_method"),
            tr("autocut_settings.col_accuracy"),
        ])
        self.station_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.station_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.station_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.station_table.verticalHeader().setVisible(False)
        header = self.station_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATION, QHeaderView.ResizeMode.Stretch)
        self.station_table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.station_table)

        buttons = QHBoxLayout()
        self.rebuild_logo_button = QPushButton(tr("autocut_settings.rebuild_logo"))
        self.rebuild_logo_button.setToolTip(tr("autocut_settings.rebuild_logo_tip"))
        self.rebuild_logo_button.clicked.connect(self._on_rebuild_logo)
        buttons.addWidget(self.rebuild_logo_button)
        self.export_lgd_button = QPushButton(tr("autocut_settings.export_lgd"))
        self.export_lgd_button.setToolTip(tr("autocut_settings.export_lgd_tip"))
        self.export_lgd_button.clicked.connect(self._on_export_lgd)
        buttons.addWidget(self.export_lgd_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        hint = QLabel(tr("autocut_settings.stations_hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return widget

    def refresh_stations(self) -> None:
        self._remember_station_methods()
        self.station_table.setRowCount(0)
        self._stations = []
        self._method_combos = []
        details = self._model_details(active_only=True)
        if self._db is not None:
            try:
                self._stations = self._db.list_stations()
            except Exception:  # noqa: BLE001 - 表が空になるだけ
                logger.warning("Could not list the stations in the history DB", exc_info=True)
        for row, station in enumerate(self._stations):
            onid, sid = station["original_network_id"], station["service_id"]
            logo = self._db.get_station_logo(onid, sid)
            imported = self._db.get_imported_logo(sid)
            if logo is not None and logo.get("logo_data") is not None:
                template = tr("autocut_settings.template_yes", count=logo.get("recordings_count") or 0)
                period = f"{_date(logo.get('valid_from'))} – {_date(logo.get('valid_to'))}"
            elif logo is not None and logo.get("generation_start"):
                template = tr("autocut_settings.template_restarting", since=_date(logo["generation_start"]))
                period = ""
            else:
                template, period = tr("autocut_settings.template_no"), ""
            imported_text = (f"{imported['file_name']} ({imported['logo_date'] or ''})" if imported is not None
                             else tr("autocut_settings.imported_none"))
            self.station_table.insertRow(row)
            key = station_key(onid, sid)
            values = [station["service_name"] or str(sid), str(station["recordings"]), template, period, imported_text,
                      "", self._accuracy_text(details, key)]
            for column, value in enumerate(values):
                self.station_table.setItem(row, column, QTableWidgetItem(value))
            combo = QComboBox()
            for method in _METHODS:
                combo.addItem(self._method_label(method), method)
            chosen = self._station_methods.get(key, learning.METHOD_DEFAULT)
            combo.setCurrentIndex(_METHODS.index(chosen) if chosen in _METHODS else 0)
            self.station_table.setCellWidget(row, COL_METHOD, combo)
            self._method_combos.append((key, combo))
        self._update_buttons()

    def _remember_station_methods(self) -> None:
        """表の方式の選択を覚える(表を作り直しても選んだままにする)。"""
        for key, combo in self._method_combos:
            method = combo.currentData()
            if method in (learning.METHOD_RULE, learning.METHOD_LEARNED):
                self._station_methods[key] = method
            else:
                self._station_methods.pop(key, None)

    @staticmethod
    def _method_label(method: str) -> str:
        if method == learning.METHOD_RULE:
            return tr("autocut_settings.method_rule")
        if method == learning.METHOD_LEARNED:
            return tr("autocut_settings.method_learned")
        return tr("autocut_settings.method_default")

    @staticmethod
    def _accuracy_text(details: Optional[dict], key: str) -> str:
        """局の一致率(確認用の録画での再現率、学習／ルール)と、学習を使えない理由。"""
        if not details:
            return tr("autocut_settings.accuracy_none")
        problem = learning.station_problem(details, key)
        st = details.get("stations", {}).get(key) or {}
        text = tr("autocut_settings.accuracy_none")
        if st.get("eval"):
            text = tr("autocut_settings.accuracy_value", learned=round(st["learned"]["recall"] * 100),
                      rule=round(st["rule"]["recall"] * 100), count=st["eval"])
        if problem == learning.REASON_FEW_RECORDINGS:
            text += tr("autocut_settings.station_few")
        elif problem == learning.REASON_RULE_BETTER:
            text += tr("autocut_settings.station_rule_better")
        return text

    def _selected_station(self):
        rows = self.station_table.selectionModel().selectedRows() if self.station_table.selectionModel() else []
        if not rows:
            return None
        return self._stations[rows[0].row()]

    def _update_buttons(self) -> None:
        selected = self._db is not None and self._selected_station() is not None
        self.rebuild_logo_button.setEnabled(selected)
        self.export_lgd_button.setEnabled(selected)

    def _on_export_lgd(self) -> None:
        """選んだ局のロゴを、選んだフォルダに .lgd で書き出す (映像の大きさごとに 1 ファイル)。"""
        from ts_smartcut.autocut.lgd_export import export_station_lgd

        station = self._selected_station()
        if station is None or self._db is None:
            return
        folder = QFileDialog.getExistingDirectory(self, tr("autocut_settings.export_lgd_title"),
                                                  str(self._db.db_path.parent))
        if not folder:
            return
        onid, sid = station["original_network_id"], station["service_id"]
        name = station["service_name"] or str(sid)
        try:
            written = export_station_lgd(self._db, onid, sid, name, Path(folder))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not export the station logo", exc_info=True)
            QMessageBox.warning(self, tr("autocut_settings.export_lgd_title"),
                                tr("autocut_settings.export_lgd_failed", error=str(e)))
            return
        if not written:
            QMessageBox.information(self, tr("autocut_settings.export_lgd_title"),
                                    tr("autocut_settings.export_lgd_none", station=name))
            return
        QMessageBox.information(self, tr("autocut_settings.export_lgd_title"),
                                tr("autocut_settings.export_lgd_done", station=name,
                                   files="\n".join(str(p) for p in written)))

    def _on_rebuild_logo(self) -> None:
        """新しいロゴの最初の録画を選んでもらい、そこから局の型を作り直す (古い型は期間付きで残す)。"""
        from ts_smartcut.autocut.logo import restart_station_logo
        from ts_smartcut.autocut.logo_change import station_recordings

        station = self._selected_station()
        if station is None or self._db is None:
            return
        onid, sid = station["original_network_id"], station["service_id"]
        recordings = station_recordings(self._db, onid, sid)
        if not recordings:
            return
        labels = []
        for r in recordings:
            agreement = (tr("autocut_settings.agreement", percent=round(r.agreement * 100))
                         if r.agreement is not None else tr("autocut_settings.agreement_unknown"))
            labels.append(f"{_date(r.recorded_at)}  {r.program_name}  {agreement}")
        name = station["service_name"] or str(sid)
        choice, ok = QInputDialog.getItem(self, tr("autocut_settings.rebuild_title"),
                                          tr("autocut_settings.rebuild_choose", station=name), labels, 0, False)
        if not ok:
            return
        since = recordings[labels.index(choice)].recorded_at
        try:
            restart_station_logo(self._db, onid, sid, since)
            self._db.clear_logo_change_dismissal(onid, sid)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not rebuild the station logo", exc_info=True)
            QMessageBox.warning(self, tr("autocut_settings.rebuild_title"), tr("logo_change.failed", error=str(e)))
            return
        logger.info("Restarted the station logo of service %s from %s (settings)", sid, since)
        self.refresh_stations()
        QMessageBox.information(self, tr("autocut_settings.rebuild_title"),
                                tr("autocut_settings.rebuild_done", station=name, since=_date(since)))

    # ---- 学習 (A8) --------------------------------------------------------------

    def _build_learning(self, settings) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        method_box = QGroupBox(tr("autocut_settings.group_method"))
        method_form = QFormLayout(method_box)
        self.method_combo = QComboBox()
        for method in (learning.METHOD_RULE, learning.METHOD_LEARNED):
            self.method_combo.addItem(self._method_label(method), method)
        self.method_combo.setCurrentIndex(1 if settings.autocut_method == learning.METHOD_LEARNED else 0)
        method_form.addRow(tr("autocut_settings.default_method"), self.method_combo)
        method_hint = QLabel(tr("autocut_settings.method_hint"))
        method_hint.setWordWrap(True)
        method_form.addRow(method_hint)
        layout.addWidget(method_box)

        learn_box = QGroupBox(tr("autocut_settings.group_learning"))
        learn_layout = QVBoxLayout(learn_box)
        self.learning_status_label = QLabel()
        self.learning_status_label.setWordWrap(True)
        self.learning_status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        learn_layout.addWidget(self.learning_status_label)
        buttons = QHBoxLayout()
        self.retrain_button = QPushButton(tr("autocut_settings.retrain"))
        self.retrain_button.setToolTip(tr("autocut_settings.retrain_tip"))
        self.retrain_button.clicked.connect(self._on_retrain)
        buttons.addWidget(self.retrain_button)
        self.clear_learning_button = QPushButton(tr("autocut_settings.clear_learning"))
        self.clear_learning_button.clicked.connect(self._on_clear_learning)
        buttons.addWidget(self.clear_learning_button)
        buttons.addStretch(1)
        learn_layout.addLayout(buttons)
        layout.addWidget(learn_box)
        layout.addStretch(1)
        return widget

    def _latest_model(self, active_only: bool = False) -> Optional[dict]:
        if self._db is None:
            return None
        try:
            return self._db.get_latest_model(active_only=active_only)
        except Exception:  # noqa: BLE001 - 表示だけ
            logger.warning("Could not read the learned model", exc_info=True)
            return None

    def _model_details(self, active_only: bool = False) -> Optional[dict]:
        row = self._latest_model(active_only)
        if row is None:
            return None
        try:
            return json.loads(row.get("details") or "{}")
        except ValueError:
            return None

    @staticmethod
    def status_text(row: Optional[dict], available: bool) -> str:
        """学習の状態の文(いちばん新しい学習の結果)。"""
        if not available:
            return tr("autocut_settings.learning_no_library")
        if row is None:
            return tr("autocut_settings.learning_none")
        try:
            details = json.loads(row.get("details") or "{}")
        except ValueError:
            details = {}
        learned, rule = details.get("learned") or {}, details.get("rule") or {}
        date = (row.get("trained_at") or "")[:10]
        if row.get("eval_recordings_count") and learned and rule:
            text = tr("autocut_settings.learning_status", date=date,
                      recordings=row.get("recordings_count") or 0, eval=row["eval_recordings_count"],
                      recall=f"{learned['recall'] * 100:.1f}", rule_recall=f"{rule['recall'] * 100:.1f}",
                      wrong=round(learned["wrong_cut"]), rule_wrong=round(rule["wrong_cut"]))
        else:
            text = tr("autocut_settings.learning_status_short", date=date, recordings=row.get("recordings_count") or 0)
        if row.get("library") != learning.LIBRARY:
            # 2026-10-30 より前の LightGBM の結果は使わない (学習し直すまでルール)
            return text + "\n" + tr("autocut_settings.learning_old_library")
        used = bool(row.get("is_active")) and row.get("feature_version") == learning.MODEL_FEATURE_VERSION
        return text + "\n" + tr("autocut_settings.learning_used" if used else "autocut_settings.learning_not_used")

    def refresh_learning(self) -> None:
        available = learning.learning_available()
        latest = self._latest_model()
        self.learning_status_label.setText(self.status_text(latest, available))
        busy = self._retrain_stop is not None
        self.retrain_button.setEnabled(self._db is not None and available and not busy)
        self.clear_learning_button.setEnabled(self._db is not None and not busy and latest is not None)

    def _on_retrain(self) -> None:
        """記録から学習し直す(裏のスレッド。中止できる)。結果はルールと比べて出す。"""
        if self._db is None or self._retrain_stop is not None:
            return
        stop = threading.Event()
        self._retrain_stop = stop
        bridge = _RetrainBridge(self)
        dialog = QProgressDialog(tr("autocut_settings.retrain_progress_work"), tr("autocut_settings.retrain_cancel"), 0, 0, self)
        dialog.setWindowTitle(tr("autocut_settings.retrain_title"))
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.canceled.connect(stop.set)
        self._retrain_dialog = dialog

        def on_stage(stage: str, done: int, total: int) -> None:
            if stage == "load" and total:
                dialog.setLabelText(tr("autocut_settings.retrain_progress_load", done=done, total=total))
            else:
                dialog.setLabelText(tr("autocut_settings.retrain_progress_work"))

        bridge.stage.connect(on_stage)
        bridge.finished.connect(self._on_retrain_finished)
        bridge.failed.connect(self._on_retrain_failed)
        db = self._db

        def emit(signal, *args) -> None:
            try:
                signal.emit(*args)
            except RuntimeError:  # 画面が先に閉じた(結果は記録 DB に残る)
                stop.set()

        def work() -> None:
            try:
                result = learning.retrain(db, stop, lambda st, d, t: emit(bridge.stage, st, d, t))
            except learning.LearningCancelled:
                emit(bridge.failed, "")
            except Exception as e:  # noqa: BLE001
                logger.warning("Retraining the automatic cut model failed", exc_info=True)
                emit(bridge.failed, str(e))
            else:
                emit(bridge.finished, result)

        self.refresh_learning()
        dialog.show()
        threading.Thread(target=work, name="autocut-retrain", daemon=True).start()

    def _end_retrain(self) -> None:
        self._retrain_stop = None
        if self._retrain_dialog is not None:
            self._retrain_dialog.canceled.disconnect()
            self._retrain_dialog.close()
            self._retrain_dialog = None
        self.refresh_learning()
        self.refresh_stations()

    def _on_retrain_finished(self, result) -> None:
        self._end_retrain()
        status = self.status_text(self._latest_model(), True)
        key = ("autocut_settings.retrain_done_used" if result.adoption_problem() is None
               else "autocut_settings.retrain_done_not_used")
        QMessageBox.information(self, tr("autocut_settings.retrain_title"), tr(key, status=status))

    def _on_retrain_failed(self, error: str) -> None:
        self._end_retrain()
        if error:
            QMessageBox.warning(self, tr("autocut_settings.retrain_title"),
                                tr("autocut_settings.retrain_failed", error=error))
        else:
            QMessageBox.information(self, tr("autocut_settings.retrain_title"),
                                    tr("autocut_settings.retrain_cancelled"))

    def _on_clear_learning(self) -> None:
        """学習の結果を消してルールに戻す(記録したカットは消さない)。確かめてからその場で行う。"""
        if self._db is None:
            return
        answer = QMessageBox.question(self, tr("autocut_settings.clear_title"), tr("autocut_settings.clear_confirm"))
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self._db.clear_models()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not clear the learned model", exc_info=True)
            QMessageBox.warning(self, tr("autocut_settings.clear_title"),
                                tr("autocut_settings.clear_failed", error=str(e)))
            return
        logger.info("Cleared the learned automatic cut model (%d rows)", count)
        self.refresh_learning()
        self.refresh_stations()

    def retraining(self) -> bool:
        """学習し直している途中か。"""
        return self._retrain_stop is not None

    def stop_retraining(self) -> None:
        if self._retrain_stop is not None:
            self._retrain_stop.set()


    # ---- 設定ダイアログから --------------------------------------------------------

    def thresholds_visible(self) -> bool:
        return self.tabs.currentIndex() == self.thresholds_tab_index

    def apply_to(self, settings) -> None:
        settings.autocut_proposals = self.proposals_check.isChecked()
        settings.notify_logo_change = self.logo_change_check.isChecked()
        settings.autocut_method = self.method_combo.currentData()
        self._remember_station_methods()
        settings.autocut_station_methods = dict(self._station_methods)
