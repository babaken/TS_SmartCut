"""保存名称のダイアログ (design doc §8、2026-10-11 利用者の要望)。

バッチ追加のたびに出し、出力ファイルの名前を決めてもらう(1本の録画に入った複数の話を
分けて変換する)。バッチリストの右クリック「名称変更」でも使う。拡張子は元の録画と同じで
変えられない。名前が既存のファイル・バッチの他の項目・元の録画と同じなら、注意を出して
ダイアログに戻る(入れた名前はそのまま)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from ts_smartcut.i18n.language_manager import tr


class OutputNameDialog(QDialog):
    def __init__(self, initial: Path, input_path: Path, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.folder = initial.parent
        self.extension = initial.suffix
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("output_name.recording", name=input_path.name)))
        layout.addWidget(QLabel(tr("output_name.folder", folder=self.folder)))
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("output_name.name")))
        self.name_edit = QLineEdit(initial.stem)
        self.name_edit.selectAll()
        row.addWidget(self.name_edit, stretch=1)
        row.addWidget(QLabel(self.extension))
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(max(640, self.sizeHint().width()), self.sizeHint().height())

    def output_path(self) -> Path:
        return self.folder / (self.name_edit.text().strip() + self.extension)


def ask_output_name(
    parent,
    initial: Path,
    input_path: Path,
    check: Callable[[Path], Optional[str]],
    title: Optional[str] = None,
) -> Optional[Path]:
    """保存名称を尋ねる。キャンセルなら None。check は誤りの説明(正しければ None)を返す。
    title が無ければ「保存名称」。"""
    title = title or tr("output_name.title")
    dialog = OutputNameDialog(initial, input_path, title, parent)
    while dialog.exec() == QDialog.DialogCode.Accepted:
        path = dialog.output_path()
        error = check(path)
        if error is None:
            return path
        QMessageBox.warning(dialog, title, error)
    return None
