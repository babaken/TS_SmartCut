"""設定ダイアログの「ショートカット」タブ (design doc §4.5.1)。

キープリセット(5 つ)を選んで、名前とキーを変える・既定のキーに戻す・使うプリセットにする。
編集はプリセットの写しに対して行い、設定ダイアログの OK で本物に入れる(キャンセルなら捨てる)。
キーは欄を選んでから押したキーをそのまま受け取る(キーボードモードの左手デバイスのボタンも
文字キーとして届く)。1 つの操作にキーは 1 つ。ほかの操作が使っていたキーは、そちらから外す。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ts_smartcut.core.shortcut_presets import ACTION_IDS, PRESET_COUNT, ShortcutPresetManager, display_key
from ts_smartcut.gui.shortcuts_dialog import action_label
from ts_smartcut.i18n.language_manager import tr

logger = logging.getLogger(__name__)


class ShortcutSettingsPanel(QWidget):
    def __init__(self, presets: ShortcutPresetManager, parent=None):
        super().__init__(parent)
        # 写しを編集する(OK まで保存しない)
        self.presets = presets.snapshot()
        self._updating = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        for index in range(PRESET_COUNT):
            self.preset_combo.addItem(self._name(index))
        preset_row.addWidget(self.preset_combo, 1)
        self.use_check = QCheckBox(tr("settings.preset_use"))
        preset_row.addWidget(self.use_check)
        form.addRow(tr("settings.preset"), preset_row)
        self.name_edit = QLineEdit()
        form.addRow(tr("settings.preset_name"), self.name_edit)
        layout.addLayout(form)

        hint = QLabel(tr("settings.shortcut_hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(len(ACTION_IDS), 3)
        self.table.setHorizontalHeaderLabels([tr("shortcuts.action"), tr("shortcuts.key"), ""])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.key_edits = {}
        for row, action in enumerate(ACTION_IDS):
            item = QTableWidgetItem(action_label(action))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item)
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setMinimumWidth(120)
            edit.keySequenceChanged.connect(lambda _seq, a=action: self._on_key_changed(a))
            self.table.setCellWidget(row, 1, edit)
            self.key_edits[action] = edit
            clear = QPushButton(tr("settings.shortcut_clear"))
            clear.clicked.connect(lambda _checked=False, a=action: self._on_clear(a))
            self.table.setCellWidget(row, 2, clear)
        self.table.setMinimumHeight(320)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        bottom.addWidget(self.status_label, 1)
        self.reset_button = QPushButton(tr("settings.preset_reset"))
        self.reset_button.clicked.connect(self._on_reset)
        bottom.addWidget(self.reset_button)
        layout.addLayout(bottom)

        self.preset_combo.currentIndexChanged.connect(self._show_preset)
        self.use_check.toggled.connect(self._on_use_toggled)
        self.name_edit.textEdited.connect(self._on_name_edited)
        self.preset_combo.setCurrentIndex(self.presets.active_index)
        self._show_preset(self.presets.active_index)

    # ---- 表示 -------------------------------------------------------------

    def _name(self, index: int) -> str:
        return self.presets.presets[index].name or tr("shortcuts.preset_default_name", n=index + 1)

    @property
    def _preset(self):
        return self.presets.presets[self.preset_combo.currentIndex()]

    def _show_preset(self, index: int) -> None:
        self._updating = True
        try:
            preset = self.presets.presets[index]
            self.use_check.setChecked(index == self.presets.active_index)
            # 使っているプリセットは外せない(どれか 1 つは使う)
            self.use_check.setEnabled(index != self.presets.active_index)
            self.name_edit.setText(preset.name)
            self.name_edit.setPlaceholderText(tr("shortcuts.preset_default_name", n=index + 1))
            for action in ACTION_IDS:
                self._show_key(action)
            self.status_label.setText("")
        finally:
            self._updating = False

    def _show_key(self, action: str) -> None:
        key = self._preset.key_for(action)
        edit = self.key_edits[action]
        was = self._updating
        self._updating = True
        try:
            edit.setKeySequence(QKeySequence(key, QKeySequence.SequenceFormat.PortableText) if key else QKeySequence())
        finally:
            self._updating = was

    # ---- 操作 -------------------------------------------------------------

    def _on_key_changed(self, action: str) -> None:
        if self._updating:
            return
        key = self.key_edits[action].keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        moved_from = self._preset.set_key(action, key)
        self._show_key(action)
        if moved_from is not None:
            self._show_key(moved_from)
            self.status_label.setText(tr("settings.shortcut_moved", shortcut=display_key(self._preset.key_for(action)),
                                         action=action_label(moved_from)))
        else:
            self.status_label.setText("")

    def _on_clear(self, action: str) -> None:
        self._preset.set_key(action, "")
        self._show_key(action)
        self.status_label.setText("")

    def _on_reset(self) -> None:
        index = self.preset_combo.currentIndex()
        answer = QMessageBox.question(self, tr("settings.preset_reset"),
                                      tr("settings.preset_reset_confirm", name=self._name(index)))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._preset.reset()
        self._show_preset(index)

    def _on_use_toggled(self, checked: bool) -> None:
        if self._updating or not checked:
            return
        self.presets.active_index = self.preset_combo.currentIndex()
        self.use_check.setEnabled(False)

    def _on_name_edited(self, text: str) -> None:
        index = self.preset_combo.currentIndex()
        self._preset.name = text.strip()
        self.preset_combo.setItemText(index, self._name(index))

    def edited_presets(self) -> ShortcutPresetManager:
        """編集したプリセット(OK の時に本物へ入れる)。"""
        return self.presets
