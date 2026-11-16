"""設定ダイアログ (design doc §23)。

design doc §23のモックアップに合わせ、タブ構成（全般/エンコード/UI/詳細）
で全設定項目を統合する。「自動カット」タブ(設計書 §31.9)の中は gui/autocut_settings_panel.py
(全般・局とロゴ・しきい値。しきい値は一覧だけで、値はバッチの後に自動で補正する)。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ts_smartcut.core import script_output
from ts_smartcut.core.hardware_encoders import get_hardware_encoders
from ts_smartcut.core.output_manager import validate_filename_pattern
from ts_smartcut.ffmpeg.smart_cut import MODE_FULL_REENCODE, MODE_SIMPLE_COPY, MODE_SMART_CUT
from ts_smartcut.gui.theme import THEME_CHOICES
from ts_smartcut.gui.autocut_settings_panel import AutoCutSettingsPanel
from ts_smartcut.gui.shortcut_settings_panel import ShortcutSettingsPanel
from ts_smartcut.i18n.language_manager import LANGUAGE_NAMES, SUPPORTED_LANGUAGES
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.errors import TSSmartCutError
from ts_smartcut.models import AppSettings
from ts_smartcut.mpv.base import AUTO_AUDIO_DEVICE

logger = logging.getLogger(__name__)

LOG_LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"]

# 処理モードの表示名（バッチリストのモード列でも使う）
PROCESSING_MODE_LABELS = {
    MODE_SMART_CUT: "Smart Cut",
    MODE_SIMPLE_COPY: "Simple Copy",
    MODE_FULL_REENCODE: "Full Re-encode",
}

# 処理モードの説明(文字の一覧の項目)
PROCESSING_MODE_DESCRIPTIONS = {
    MODE_SMART_CUT: "settings.mode_smart_cut",
    MODE_SIMPLE_COPY: "settings.mode_simple_copy",
    MODE_FULL_REENCODE: "settings.mode_full_reencode",
}


# 出力形式の表示名(バッチリストのモード列・右クリックのメニューでも使う。設計書 §6.9)
OUTPUT_FORMAT_LABELS = {
    script_output.OUTPUT_TS: "TS",
    script_output.OUTPUT_AVS: "AVS (AviSynth)",
    script_output.OUTPUT_VPY: "VPY (VapourSynth)",
}


# 再エンコードを行うモード(スレッド数・ハードウェアエンコーダーが関係する)
_MODE_REENCODES = {MODE_SMART_CUT, MODE_FULL_REENCODE}


class SettingsDialog(QDialog):
    def __init__(
        self, settings: AppSettings, parent=None,
        audio_devices: Optional[List[Tuple[str, str]]] = None,
        history_db=None,
        shortcut_presets=None,
    ):
        """audio_devicesは音声の出力先の一覧(名前, 表示名)。mpvが使えない時はNone。
        history_db は自動カットの記録 DB(局とロゴ・しきい値。使えない時は None)。
        shortcut_presets はキープリセット(ShortcutPresetManager。None ならショートカットのタブを出さない)。"""
        super().__init__(parent)
        self.setWindowTitle(tr("settings.title"))
        self._settings = copy.copy(settings)
        self._audio_devices = audio_devices or []

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_general_tab(), tr("settings.tab_general"))
        tabs.addTab(self._build_preview_tab(), tr("settings.tab_preview"))
        tabs.addTab(self._build_encode_tab(), tr("settings.tab_encode"))
        tabs.addTab(self._build_ui_tab(), "UI")
        self.shortcut_panel = ShortcutSettingsPanel(shortcut_presets) if shortcut_presets is not None else None
        if self.shortcut_panel is not None:
            tabs.addTab(self.shortcut_panel, tr("settings.tab_shortcuts"))
        tabs.addTab(self._build_advanced_tab(), tr("settings.tab_advanced"))
        self.autocut_panel = AutoCutSettingsPanel(self._settings, history_db)
        self.threshold_panel = self.autocut_panel.threshold_panel
        self._autocut_tab_index = tabs.addTab(self.autocut_panel, tr("settings.tab_autocut"))
        self._tabs = tabs
        tabs.currentChanged.connect(self._size_to_current_tab)
        self.autocut_panel.tabs.currentChanged.connect(self._size_to_current_tab)
        self._size_to_current_tab()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.recent_files_count_spin = QSpinBox()
        self.recent_files_count_spin.setRange(1, 50)
        self.recent_files_count_spin.setValue(self._settings.recent_files_count)
        form.addRow(tr("settings.recent_files_count"), self.recent_files_count_spin)

        self.output_pattern_edit = QLineEdit(self._settings.output_filename_pattern)
        self.output_pattern_edit.setToolTip(tr("settings.output_pattern_tip"))
        form.addRow(tr("settings.output_pattern"), self.output_pattern_edit)

        self.output_dir_edit, output_dir_row = self._output_dir_row(self._settings.default_output_dir)
        form.addRow(tr("settings.output_dir"), output_dir_row)



        return widget

    def _build_encode_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self._encode_form = form

        # 出力形式(設計書 §6.9): TS は動画を出力し、AVS／VPY はカット情報のスクリプトを書く
        self.output_format_combo = QComboBox()
        for output_format, label in OUTPUT_FORMAT_LABELS.items():
            self.output_format_combo.addItem(label, output_format)
        index = self.output_format_combo.findData(self._settings.output_format)
        self.output_format_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(tr("settings.output_format"), self.output_format_combo)
        self.output_format_description = QLabel()
        self.output_format_description.setWordWrap(True)
        form.addRow(self.output_format_description)
        # テンプレートのファイル(空 = 同梱の既定)。選んだ形式の行だけ出す
        self.avs_template_edit, self._avs_template_row = self._template_row(
            self._settings.avs_template, "AviSynth (*.avs)")
        form.addRow(tr("settings.avs_template"), self._avs_template_row)
        self.vpy_template_edit, self._vpy_template_row = self._template_row(
            self._settings.vpy_template, "VapourSynth (*.vpy)")
        form.addRow(tr("settings.vpy_template"), self._vpy_template_row)

        self.processing_mode_combo = QComboBox()
        for mode, label in PROCESSING_MODE_LABELS.items():
            self.processing_mode_combo.addItem(label, mode)
        index = self.processing_mode_combo.findData(self._settings.default_processing_mode)
        self.processing_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(tr("settings.processing_mode"), self.processing_mode_combo)

        self.processing_mode_description = QLabel()
        self.processing_mode_description.setWordWrap(True)
        form.addRow(self.processing_mode_description)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 32)
        self.threads_spin.setValue(self._settings.cpu_threads)
        form.addRow(tr("settings.threads"), self.threads_spin)

        # ハードウェアエンコーダーは初回起動時に自動で判定する(選択項目はない)。
        # 再エンコードのコーデック・走査方式・画質は元ソースに合わせて自動で決まる。
        self.hardware_label = QLabel()
        self.hardware_label.setWordWrap(True)
        self.redetect_button = QPushButton(tr("settings.redetect"))
        self.redetect_button.clicked.connect(self._on_redetect_hardware)
        hardware_box = QVBoxLayout()
        hardware_box.addWidget(self.hardware_label)
        hardware_box.addWidget(self.redetect_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self._hardware_row = QWidget()
        self._hardware_row.setLayout(hardware_box)
        form.addRow(tr("settings.hardware"), self._hardware_row)
        self._show_hardware(get_hardware_encoders(self._settings))

        self.processing_mode_combo.currentIndexChanged.connect(self._update_encode_controls)
        self.output_format_combo.currentIndexChanged.connect(self._update_encode_controls)
        self._update_encode_controls()

        return widget

    def _output_dir_row(self, path):
        """出力先のフォルダの欄と「参照」ボタン。空なら元の録画と同じフォルダ。"""
        edit = QLineEdit(str(path) if path else "")
        edit.setPlaceholderText(tr("settings.output_dir_default"))
        button = QPushButton(tr("settings.template_browse"))

        def browse() -> None:
            name = QFileDialog.getExistingDirectory(self, tr("settings.output_dir_choose"), edit.text())
            if name:
                edit.setText(name)

        button.clicked.connect(browse)
        box = QHBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(edit, 1)
        box.addWidget(button)
        row = QWidget()
        row.setLayout(box)
        return edit, row

    def _template_row(self, path, file_filter: str):
        """テンプレートのファイルの欄と「参照」ボタン。空なら同梱の既定のテンプレート。"""
        edit = QLineEdit(str(path) if path else "")
        edit.setPlaceholderText(tr("settings.template_default"))
        button = QPushButton(tr("settings.template_browse"))

        def browse() -> None:
            name, _ = QFileDialog.getOpenFileName(self, tr("settings.template_choose"), edit.text(), file_filter)
            if name:
                edit.setText(name)

        button.clicked.connect(browse)
        box = QHBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(edit, 1)
        box.addWidget(button)
        row = QWidget()
        row.setLayout(box)
        return edit, row

    def _show_hardware(self, detection) -> None:
        self.hardware_label.setText(detection.describe())

    def _on_redetect_hardware(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._show_hardware(get_hardware_encoders(self._settings, force=True))
        finally:
            QApplication.restoreOverrideCursor()

    def _update_encode_controls(self) -> None:
        """選択中の出力形式・処理モードで使われる設定だけを表示する。AVS／VPY は処理モードを使わない
        (バッチリストで TS に戻した項目は、ここの処理モードを使う)。"""
        output_format = self.output_format_combo.currentData()
        self.output_format_description.setText(tr(f"settings.output_format_{output_format}"))
        self._encode_form.setRowVisible(self._avs_template_row, output_format == script_output.OUTPUT_AVS)
        self._encode_form.setRowVisible(self._vpy_template_row, output_format == script_output.OUTPUT_VPY)
        mode = self.processing_mode_combo.currentData()
        self.processing_mode_description.setText(tr(PROCESSING_MODE_DESCRIPTIONS[mode]))
        reencodes = mode in _MODE_REENCODES
        self._encode_form.setRowVisible(self.threads_spin, reencodes)
        self._encode_form.setRowVisible(self._hardware_row, reencodes)
        self._fit_height_later()

    def _size_to_current_tab(self) -> None:
        """しきい値の一覧は、自動カットのタブを表示している時だけ全部見える大きさにする(いつもそう
        すると、ほかのタブまで大きくなった)。表示中のタブに合わせて広げる。"""
        self.threshold_panel.set_expanded(self._tabs.currentIndex() == self._autocut_tab_index
                                          and self.autocut_panel.thresholds_visible())
        self._fit_height_later()

    def _fit_height_later(self) -> None:
        # タブの切り替え・表示する行の変化が反映されてから測る
        QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        """表示中のタブに必要な高さまでダイアログを広げる。ダイアログは開いた時の
        タブ(全般)に合わせた高さで表示され、折り返す説明文があるエンコードタブに
        切り替えても大きくならず、ハードウェアの判定結果の行が上下で切れていた
        (画面のスクリーンショットで判明、2026-10-06)。"""
        if not self.isVisible():
            return
        layout = self.layout()
        layout.activate()
        needed = layout.sizeHint().height()
        if layout.hasHeightForWidth():
            needed = max(needed, layout.heightForWidth(self.width()))
        width = max(self.width(), layout.sizeHint().width())
        if self.height() < needed or self.width() < width:
            self.resize(width, max(self.height(), needed))

    def _build_preview_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.audio_device_combo = QComboBox()
        devices = [(n, d) for n, d in self._audio_devices if n != AUTO_AUDIO_DEVICE]
        self.audio_device_combo.addItem(tr("settings.audio_auto"), AUTO_AUDIO_DEVICE)
        for name, description in devices:
            self.audio_device_combo.addItem(description, name)
        saved = self._settings.audio_output_device
        if saved in ("", "default"):
            saved = AUTO_AUDIO_DEVICE
        index = self.audio_device_combo.findData(saved)
        if index < 0:
            # 保存済みの出力先が今は無い(外したUSB機器等)。選択は残し、再生は自動になる
            self.audio_device_combo.addItem(tr("settings.audio_not_connected", name=saved), saved)
            index = self.audio_device_combo.count() - 1
        self.audio_device_combo.setCurrentIndex(index)
        if not self._audio_devices:
            self.audio_device_combo.setToolTip(tr("settings.audio_no_mpv"))
        form.addRow(tr("settings.audio_device"), self.audio_device_combo)

        self.show_waveform_check = QCheckBox(tr("settings.show_waveform"))
        self.show_waveform_check.setChecked(self._settings.show_waveform)
        form.addRow(self.show_waveform_check)

        return widget

    def _build_ui_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(THEME_CHOICES)  # system = OS の設定に従う
        self.theme_combo.setCurrentText(self._settings.theme)
        form.addRow(tr("settings.theme"), self.theme_combo)

        self.language_combo = QComboBox()
        for code in SUPPORTED_LANGUAGES:
            self.language_combo.addItem(LANGUAGE_NAMES.get(code, code), code)
        index = self.language_combo.findData(self._settings.language)
        self.language_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(tr("settings.language"), self.language_combo)

        return widget

    def _build_advanced_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.log_enabled_check = QCheckBox(tr("settings.log_enabled"))
        self.log_enabled_check.setChecked(self._settings.log_enabled)
        form.addRow(self.log_enabled_check)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        self.log_level_combo.setCurrentText(self._settings.log_level)
        form.addRow(tr("settings.log_level"), self.log_level_combo)

        return widget

    def accept(self) -> None:
        # 誤ったファイル名パターンは保存しない(バッチ処理の途中で気づくより早く知らせる)
        error = validate_filename_pattern(self.output_pattern_edit.text())
        if error:
            QMessageBox.warning(self, tr("settings.title"), error)
            self.output_pattern_edit.setFocus()
            return
        # 無いフォルダの出力先も保存しない(処理の時に初めて気づくより早く知らせる)
        output_dir = self.output_dir_edit.text().strip()
        if output_dir and not Path(output_dir).is_dir():
            QMessageBox.warning(self, tr("settings.title"), tr("settings.output_dir_missing", path=output_dir))
            self.output_dir_edit.setFocus()
            return
        # 誤ったテンプレート(読めない・置き換える文字が無い)も保存しない
        for output_format, edit in ((script_output.OUTPUT_AVS, self.avs_template_edit),
                                    (script_output.OUTPUT_VPY, self.vpy_template_edit)):
            if not edit.text().strip():
                continue
            try:
                script_output.check_template(script_output.load_template(output_format, Path(edit.text().strip())))
            except TSSmartCutError as e:
                QMessageBox.warning(self, tr("settings.title"), e.message)
                edit.setFocus()
                return
        super().accept()

    def get_settings(self) -> AppSettings:
        """OKで確定した設定値を反映した新しいAppSettingsを返す。"""
        s = self._settings
        s.recent_files_count = self.recent_files_count_spin.value()
        s.output_filename_pattern = self.output_pattern_edit.text()
        output_dir = self.output_dir_edit.text().strip()
        s.default_output_dir = Path(output_dir) if output_dir else None

        s.audio_output_device = self.audio_device_combo.currentData()
        s.show_waveform = self.show_waveform_check.isChecked()

        s.output_format = self.output_format_combo.currentData()
        s.avs_template = Path(self.avs_template_edit.text().strip()) if self.avs_template_edit.text().strip() else None
        s.vpy_template = Path(self.vpy_template_edit.text().strip()) if self.vpy_template_edit.text().strip() else None
        s.default_processing_mode = self.processing_mode_combo.currentData()
        s.cpu_threads = self.threads_spin.value()

        s.theme = self.theme_combo.currentText()
        s.language = self.language_combo.currentData()

        s.log_enabled = self.log_enabled_check.isChecked()
        s.log_level = self.log_level_combo.currentText()

        self.autocut_panel.apply_to(s)

        return s

    def get_shortcut_presets(self):
        """OK で確定したキープリセット(ショートカットのタブが無ければ None)。"""
        return self.shortcut_panel.edited_presets() if self.shortcut_panel is not None else None
