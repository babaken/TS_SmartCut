import pytest

from ts_smartcut.gui.theme import DEFAULT_THEME, THEMES, ThemeManager


def test_theme_dicts_have_required_keys():
    required = {"bg", "fg", "select_bg", "select_fg", "timeline_bg", "keep_region", "remove_region"}
    for name in ("light", "dark"):
        assert required.issubset(THEMES[name].keys())


def test_get_theme_known_names():
    assert ThemeManager.get_theme("light")["bg"] == "#ffffff"
    assert ThemeManager.get_theme("dark")["bg"] == "#1e1e1e"


def test_get_theme_unknown_falls_back_to_default():
    assert ThemeManager.get_theme("not_a_real_theme") == ThemeManager.get_theme(DEFAULT_THEME)


def test_apply_theme_returns_theme_and_configures_palette(qapp):
    theme = ThemeManager.apply_theme(qapp, "dark")
    assert theme == THEMES["dark"]
    assert qapp.palette().color(qapp.palette().currentColorGroup(), qapp.palette().ColorRole.Window).name() == THEMES["dark"]["bg"]
    ThemeManager.apply_theme(qapp, "light")  # 他テストへの影響を避けるため既定へ戻す


def test_apply_theme_unknown_name_uses_default(qapp):
    theme = ThemeManager.apply_theme(qapp, "bogus")
    assert theme == THEMES[DEFAULT_THEME]


def test_apply_theme_overrides_os_color_scheme(qapp):
    # OSがダークモードでもライトテーマならスタイル側もライトで描かせる
    # (Windows 11でメニューバー等が白地に白文字になっていた)。
    from PySide6.QtCore import Qt

    hints = qapp.styleHints()
    if not hasattr(hints, "setColorScheme"):
        return  # Qt 6.8未満
    # Linuxのプラットフォーム(wayland/xcb/offscreen)は要求を無視してOSの値を
    # 返し続ける。そこではFusionスタイルがパレットどおりに描くので問題は起きない。
    for scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
        hints.setColorScheme(scheme)
        if hints.colorScheme() != scheme:
            ThemeManager.apply_theme(qapp, DEFAULT_THEME)
            pytest.skip("このプラットフォームは配色の指定に従わない")
    try:
        ThemeManager.apply_theme(qapp, "light")
        assert hints.colorScheme() == Qt.ColorScheme.Light
        ThemeManager.apply_theme(qapp, "dark")
        assert hints.colorScheme() == Qt.ColorScheme.Dark
    finally:
        ThemeManager.apply_theme(qapp, DEFAULT_THEME)


def test_system_theme_follows_the_os_color_scheme(qapp):
    """「OS に従う」(system): 配色の固定を外し、OS のライト/ダークを使う(2026-10-09、§24)。"""
    from PySide6.QtCore import Qt

    from ts_smartcut.gui.theme import SYSTEM_THEME, THEME_CHOICES, resolve_theme_name

    assert SYSTEM_THEME in THEME_CHOICES
    try:
        theme = ThemeManager.apply_theme(qapp, SYSTEM_THEME)
        hints = qapp.styleHints()
        expected = "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
        assert resolve_theme_name(SYSTEM_THEME, qapp) == expected
        assert theme == THEMES[expected]
        assert ThemeManager.get_theme(SYSTEM_THEME) == THEMES[expected]
    finally:
        ThemeManager.apply_theme(qapp, DEFAULT_THEME)
    assert resolve_theme_name("dark") == "dark" and resolve_theme_name("bogus") == DEFAULT_THEME
