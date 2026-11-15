"""キープリセット (設計書 §4.5.1): 保存・読み込み・付け替え・リセットと、画面への反映。"""

import json

import pytest
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMessageBox

from ts_smartcut.core.shortcut_presets import (
    ACTION_IDS,
    DEFAULT_KEYS,
    PRESET_COUNT,
    ShortcutPresetManager,
)


@pytest.fixture
def presets_path(tmp_path):
    return tmp_path / "shortcut_presets.json"


def test_without_a_file_every_preset_has_the_default_keys(qapp, presets_path):
    manager = ShortcutPresetManager(presets_path)
    assert len(manager.presets) == PRESET_COUNT == 5
    assert manager.active_index == 0
    for preset in manager.presets:
        assert preset.keys == DEFAULT_KEYS
        assert preset.name == ""
    assert not presets_path.exists()  # 読むだけでは書かない


def test_the_defaults_are_the_keys_used_before(qapp):
    assert DEFAULT_KEYS["play_pause"] == "Space"
    assert DEFAULT_KEYS["set_start"] == "S"
    assert DEFAULT_KEYS["open"] == "Ctrl+O"
    assert DEFAULT_KEYS["exit"] == "Ctrl+Q"
    # 新しい操作(案の承認・却下)は今までキーが無かったので割り当てなし
    assert DEFAULT_KEYS["accept_proposal"] == "" and DEFAULT_KEYS["reject_proposal"] == ""
    used = [k for k in DEFAULT_KEYS.values() if k]
    assert len(used) == len(set(used))


def test_names_keys_and_the_active_preset_are_saved(qapp, presets_path):
    manager = ShortcutPresetManager(presets_path)
    manager.presets[2].name = "左手デバイス"
    manager.presets[2].set_key("frame_back", "a")
    manager.set_active(2)

    loaded = ShortcutPresetManager(presets_path)
    assert loaded.active_index == 2
    assert loaded.active.name == "左手デバイス"
    assert loaded.key_for("frame_back") == "A"
    assert loaded.presets[0].keys == DEFAULT_KEYS


def test_a_key_used_by_another_action_moves(qapp, presets_path):
    preset = ShortcutPresetManager(presets_path).presets[0]
    moved_from = preset.set_key("frame_back", "S")  # S は Start設定のキー
    assert moved_from == "set_start"
    assert preset.key_for("frame_back") == "S"
    assert preset.key_for("set_start") == ""
    assert preset.set_key("frame_forward", "F") is None


def test_one_key_per_action(qapp, presets_path):
    preset = ShortcutPresetManager(presets_path).presets[0]
    preset.set_key("play_pause", "G")
    assert preset.key_for("play_pause") == "G"
    assert "Space" not in preset.keys.values()


def test_reset_restores_the_default_keys_and_keeps_the_name(qapp, presets_path):
    preset = ShortcutPresetManager(presets_path).presets[1]
    preset.name = "B"
    preset.set_key("undo", "N")
    preset.set_key("mute", "")
    preset.reset()
    assert preset.keys == DEFAULT_KEYS
    assert preset.name == "B"


def test_a_broken_file_falls_back_to_the_defaults(qapp, presets_path):
    presets_path.write_text("{not json", encoding="utf-8")
    manager = ShortcutPresetManager(presets_path)
    assert manager.active.keys == DEFAULT_KEYS


def test_unknown_actions_bad_keys_and_missing_actions(qapp, presets_path):
    presets_path.write_text(json.dumps({
        "version": 1, "active": 9,
        "presets": [{"name": 3, "keys": {"no_such_action": "Z", "frame_back": "S", "set_end": "Ctrl+K, Ctrl+L"}}],
    }), encoding="utf-8")
    manager = ShortcutPresetManager(presets_path)
    assert manager.active_index == 0  # 範囲外は 0
    preset = manager.presets[0]
    assert preset.name == ""
    assert "no_such_action" not in preset.keys
    assert preset.key_for("frame_back") == "S"
    # 書かれていない Start設定 の既定 S は frame_back が使っているので割り当てなし
    assert preset.key_for("set_start") == ""
    assert preset.key_for("set_end") == ""  # 2 打鍵のキーは受け付けない
    assert preset.key_for("play_pause") == "Space"  # 書かれていない操作は既定
    assert set(preset.keys) == set(ACTION_IDS)


# ---- 設定のタブ(画面の窓への反映は test_gui_main_window.py。mpv を増やさない) ----


def test_the_settings_tab_edits_a_copy_until_ok(qapp, presets_path, monkeypatch):
    from ts_smartcut.gui.shortcut_settings_panel import ShortcutSettingsPanel

    manager = ShortcutPresetManager(presets_path)
    panel = ShortcutSettingsPanel(manager)
    panel.preset_combo.setCurrentIndex(2)
    panel.name_edit.setText("左手デバイス")
    panel.name_edit.textEdited.emit("左手デバイス")
    assert panel.preset_combo.itemText(2) == "左手デバイス"

    # キーの欄に S を入れると、S を使っていた Start設定 からは外れる
    panel.key_edits["frame_back"].setKeySequence(QKeySequence("S"))
    assert panel.key_edits["set_start"].keySequence().isEmpty()
    assert panel.status_label.text()
    panel.use_check.setChecked(True)
    assert manager.active_index == 0 and manager.presets[2].name == ""  # 本物はまだそのまま

    edited = panel.edited_presets()
    assert edited.active_index == 2
    assert edited.presets[2].key_for("frame_back") == "S"
    assert edited.presets[2].key_for("set_start") == ""

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel._on_reset()
    assert edited.presets[2].keys == DEFAULT_KEYS
    assert edited.presets[2].name == "左手デバイス"
    assert panel.key_edits["set_start"].keySequence().toString() == "S"

    panel._on_clear("play_pause")
    assert edited.presets[2].key_for("play_pause") == ""

    manager.replace_with(edited)
    assert ShortcutPresetManager(presets_path).active.name == "左手デバイス"


def test_the_settings_dialog_has_the_shortcut_tab_only_when_given_presets(qapp, presets_path):
    from PySide6.QtWidgets import QTabWidget

    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.models import AppSettings

    def tab_names(dialog):
        tabs = dialog.findChild(QTabWidget)
        return [tabs.tabText(i) for i in range(tabs.count())]

    with_tab = SettingsDialog(AppSettings(), shortcut_presets=ShortcutPresetManager(presets_path))
    assert "ショートカット" in tab_names(with_tab)
    assert with_tab.get_shortcut_presets() is not None
    without = SettingsDialog(AppSettings())
    assert "ショートカット" not in tab_names(without)
    assert without.get_shortcut_presets() is None
