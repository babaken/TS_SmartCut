"""システム情報ダイアログ (design doc §22)。"""

from __future__ import annotations

import platform
import sys

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QTextEdit,
    QVBoxLayout,
)

from ts_smartcut.autocut.native import native_problem
from ts_smartcut.core.hardware_encoders import get_hardware_encoders
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.models import AppSettings
from ts_smartcut.system.dependency_checker import DependencyChecker


def _native_text() -> str:
    """自動カットの計算の部品(ts_smartcut_native)の版、使えなければ理由。"""
    problem = native_problem()
    if problem is not None:
        return problem.message
    import ts_smartcut_native

    return f"ts_smartcut_native {ts_smartcut_native.VERSION}"


def build_system_info_text(settings: AppSettings) -> str:
    versions = DependencyChecker.get_versions(
        ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path,
        mpv_path=settings.mpv_path,
    )
    hardware = get_hardware_encoders(settings).describe()

    lines = [
        tr("system_info.title"),
        "――――――――――――――――――――",
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {sys.version.split()[0]}",
        "――――――――――――――――――――",
        tr("system_info.external_tools"),
        f"  ffmpeg: {versions['ffmpeg']}",
        f"  ffprobe: {versions['ffprobe']}",
        f"  mpv: {versions['mpv']}",
        "  " + tr("system_info.native") + ": " + _native_text(),
        "――――――――――――――――――――",
        tr("system_info.hardware"),
        *[f"  {line}" for line in hardware.splitlines()],
        "――――――――――――――――――――",
        tr("system_info.settings"),
        f"  Threads: {settings.cpu_threads if settings.cpu_threads else 'auto'}",
        "  " + tr("system_info.language", value=settings.language),
        "  " + tr("system_info.theme", value=settings.theme),
    ]
    return "\n".join(lines)


class SystemInfoDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("system_info.title"))
        self.resize(480, 420)

        layout = QVBoxLayout(self)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(build_system_info_text(settings))
        layout.addWidget(text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        copy_button = buttons.addButton(tr("system_info.copy"), QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(text.toPlainText()))
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
