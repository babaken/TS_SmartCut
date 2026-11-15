"""バージョン情報 (design doc §4.2 ヘルプメニュー)。

外部ツール(ffmpeg/ffprobe/mpv)のバージョンやGPU対応状況は
「システム情報」(design doc §12.3, §22) の担当なので、ここでは
アプリ自身とGUIランタイムの情報だけを表示する。"""

from __future__ import annotations

import html
import sys

import PySide6
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QMessageBox, QWidget

from ts_smartcut import __version__
from ts_smartcut.i18n.language_manager import LanguageManager

REPOSITORY_URL = "https://github.com/babaken/TS_SmartCut"


def build_about_html(lang: LanguageManager) -> str:
    rows = [
        ("Python", sys.version.split()[0]),
        ("PySide6", PySide6.__version__),
        ("Qt", qVersion()),
    ]
    runtime = "<br>".join(f"{name}: {html.escape(version)}" for name, version in rows)
    return (
        f"<h3>TS_SmartCut {html.escape(__version__)}</h3>"
        f"<p>{html.escape(lang.get('messages.about_description'))}</p>"
        f"<p>{runtime}</p>"
        f"<p>{html.escape(lang.get('messages.about_external_tools'))}</p>"
        f'<p><a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a></p>'
    )


def show_about_dialog(parent: QWidget, lang: LanguageManager) -> None:
    QMessageBox.about(parent, lang.get("menu.version_info"), build_about_html(lang))
