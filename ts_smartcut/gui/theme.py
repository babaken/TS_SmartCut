"""ライト/ダークテーマ管理 (design doc §24、Qt6/QPalette版)。

QPaletteで標準ウィジェット全体の配色を切り替える。タイムライン等の
カスタム描画部分（QPainterで直接描画する箇所）はQPaletteでカバーされ
ないため、THEMES辞書から直接色を取得して使う。
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

THEMES: Dict[str, Dict[str, str]] = {
    "light": {
        "bg": "#ffffff",
        "fg": "#000000",
        "select_bg": "#0078d7",
        "select_fg": "#ffffff",
        "timeline_bg": "#f0f0f0",
        "keep_region": "#90ee90",
        "remove_region": "#ff6b6b",
    },
    "dark": {
        "bg": "#1e1e1e",
        "fg": "#ffffff",
        "select_bg": "#0078d7",
        "select_fg": "#ffffff",
        "timeline_bg": "#2d2d2d",
        "keep_region": "#4a7c4a",
        "remove_region": "#8b4545",
    },
}

DEFAULT_THEME = "light"
# OS のライト/ダークの設定に従う(2026-10-09 利用者の決定、§24)。設定値で、配色は THEMES の
# どちらか(resolve_theme_name)
SYSTEM_THEME = "system"
THEME_CHOICES = ("light", "dark", SYSTEM_THEME)


def resolve_theme_name(theme_name: str, app: QApplication = None) -> str:
    """設定のテーマ名を、実際に使う配色の名前(light / dark)にする。"system" は OS の設定。"""
    if theme_name != SYSTEM_THEME:
        return theme_name if theme_name in THEMES else DEFAULT_THEME
    app = app or QApplication.instance()
    hints = app.styleHints() if app is not None else None
    if hints is not None and hasattr(hints, "colorScheme") and hints.colorScheme() == Qt.ColorScheme.Dark:
        return "dark"
    return "light"


class ThemeManager:
    THEMES = THEMES

    @staticmethod
    def get_theme(theme_name: str) -> Dict[str, str]:
        return ThemeManager.THEMES.get(resolve_theme_name(theme_name), ThemeManager.THEMES[DEFAULT_THEME])

    @staticmethod
    def apply_theme(app: QApplication, theme_name: str) -> Dict[str, str]:
        """テーマをアプリケーション全体のQPaletteへ適用する。
        適用したテーマの配色辞書を返す（カスタム描画部分の手動配色に使う）。"""
        # OS側のライト/ダーク設定に関わらずテーマに合わせる。Windows 11の
        # windows11スタイルはOSがダークモードだとメニューバーやボタンの文字を
        # 明るい色で描くため、ライトテーマのパレットと組み合わさると白地に白文字に
        # なっていた(Windows実機で確認)。setColorSchemeはQt 6.8以降。
        # 同じ値でもsetColorSchemeは全ウィンドウへのテーマ再適用を走らせるため、
        # 変わる時だけ呼ぶ(mpvとの相性問題はtests/conftest.pyの
        # mpv_without_audio_output参照)。
        # "system"(OS に従う)は固定を外して OS の値を使う(unsetColorScheme も Qt 6.8 以降)。
        hints = app.styleHints()
        if theme_name == SYSTEM_THEME:
            if hasattr(hints, "unsetColorScheme"):
                hints.unsetColorScheme()
        else:
            scheme = Qt.ColorScheme.Dark if theme_name == "dark" else Qt.ColorScheme.Light
            if hasattr(hints, "setColorScheme") and hints.colorScheme() != scheme:
                hints.setColorScheme(scheme)
        theme_name = resolve_theme_name(theme_name, app)
        theme = ThemeManager.THEMES[theme_name]

        palette = QPalette()
        bg = QColor(theme["bg"])
        fg = QColor(theme["fg"])
        select_bg = QColor(theme["select_bg"])
        select_fg = QColor(theme["select_fg"])

        palette.setColor(QPalette.ColorRole.Window, bg)
        palette.setColor(QPalette.ColorRole.WindowText, fg)
        palette.setColor(QPalette.ColorRole.Base, bg)
        palette.setColor(QPalette.ColorRole.AlternateBase, bg.lighter(110) if theme_name == "dark" else bg.darker(105))
        palette.setColor(QPalette.ColorRole.Text, fg)
        palette.setColor(QPalette.ColorRole.Button, bg)
        palette.setColor(QPalette.ColorRole.ButtonText, fg)
        palette.setColor(QPalette.ColorRole.ToolTipBase, bg)
        palette.setColor(QPalette.ColorRole.ToolTipText, fg)
        palette.setColor(QPalette.ColorRole.Highlight, select_bg)
        palette.setColor(QPalette.ColorRole.HighlightedText, select_fg)

        app.setPalette(palette)
        return theme
