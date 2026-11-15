"""キーボードショートカット一覧ダイアログ (design doc §25.1)。

使っているキープリセット(設計書 §4.5.1)のキーを出す。割り当てなしの操作は「（なし）」。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ts_smartcut.core.shortcut_presets import ACTION_IDS, ShortcutPreset, display_key
from ts_smartcut.i18n.language_manager import tr


def action_label(action: str) -> str:
    """操作の名前(文字の一覧の shortcuts.<操作>)。"""
    return tr(f"shortcuts.{action}")


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None, preset: Optional[ShortcutPreset] = None, preset_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr("shortcuts.title"))
        self.resize(400, 560)
        preset = preset or ShortcutPreset()

        layout = QVBoxLayout(self)
        if preset_name:
            layout.addWidget(QLabel(tr("shortcuts.preset_label", name=preset_name)))

        table = QTableWidget(len(ACTION_IDS), 2)
        table.setHorizontalHeaderLabels([tr("shortcuts.key"), tr("shortcuts.action")])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, action in enumerate(ACTION_IDS):
            key = display_key(preset.key_for(action)) or tr("shortcuts.unassigned")
            table.setItem(row, 0, QTableWidgetItem(key))
            table.setItem(row, 1, QTableWidgetItem(action_label(action)))
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)
