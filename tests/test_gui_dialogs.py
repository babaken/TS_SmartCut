import pytest
from PySide6.QtWidgets import QDialogButtonBox, QTabWidget

import PySide6

from ts_smartcut import __version__
from ts_smartcut.gui.about_dialog import build_about_html
from ts_smartcut.gui.settings_dialog import SettingsDialog
from ts_smartcut.i18n.language_manager import LanguageManager
from ts_smartcut.core.shortcut_presets import ACTION_IDS
from ts_smartcut.gui.shortcuts_dialog import ShortcutsDialog
from ts_smartcut.gui.system_info_dialog import SystemInfoDialog, build_system_info_text
from ts_smartcut.models import AppSettings


def test_shortcuts_dialog_has_expected_title(qapp):
    dialog = ShortcutsDialog()
    assert dialog.windowTitle() == "キーボードショートカット一覧"
    dialog.close()
    assert len(ACTION_IDS) > 0


def test_shortcuts_dialog_table_row_count(qapp):
    dialog = ShortcutsDialog()
    tables = [c for c in dialog.children() if type(c).__name__ == "QTableWidget"]
    assert len(tables) == 1
    assert tables[0].rowCount() == len(ACTION_IDS)
    dialog.close()


def test_build_system_info_text_includes_key_sections():
    text = build_system_info_text(AppSettings())
    assert "OS:" in text
    assert "Python:" in text
    assert "ffmpeg:" in text
    assert "ffprobe:" in text
    assert "mpv:" in text
    assert "ハードウェアエンコーダー" in text


def test_system_info_dialog_shows_real_tool_versions(qapp):
    dialog = SystemInfoDialog(AppSettings())
    texts = [c for c in dialog.children() if type(c).__name__ == "QTextEdit"]
    assert len(texts) == 1
    content = texts[0].toPlainText()
    assert "Not found" not in content  # ffmpeg/ffprobe/mpvはこの環境で利用可能
    dialog.close()


def test_settings_dialog_roundtrips_values(qapp):
    settings = AppSettings(theme="light", cpu_threads=0)
    dialog = SettingsDialog(settings)

    dialog.theme_combo.setCurrentText("dark")
    dialog.threads_spin.setValue(4)

    updated = dialog.get_settings()

    assert updated.theme == "dark"
    assert updated.cpu_threads == 4
    # 元のsettingsオブジェクトは変更されない(copyして操作する設計)
    assert settings.theme == "light"
    dialog.close()


def test_settings_dialog_has_ok_cancel_buttons(qapp):
    dialog = SettingsDialog(AppSettings())
    buttons = [c for c in dialog.children() if type(c).__name__ == "QDialogButtonBox"]
    assert len(buttons) == 1
    standard_buttons = buttons[0].standardButtons()
    assert standard_buttons & QDialogButtonBox.StandardButton.Ok
    assert standard_buttons & QDialogButtonBox.StandardButton.Cancel
    dialog.close()


def test_about_html_includes_versions_and_description():
    text = build_about_html(LanguageManager("jp"))
    assert f"TS_SmartCut {__version__}" in text
    assert f"PySide6: {PySide6.__version__}" in text
    assert "Smart Cut編集ツール" in text
    assert "システム情報" in text


def test_about_html_is_translated():
    text = build_about_html(LanguageManager("en"))
    assert "Smart Cut editor" in text
    assert "System Info" in text


def _row_visible(dialog, widget):
    return dialog._encode_form.isRowVisible(widget)


def test_settings_dialog_default_processing_mode_is_smart_cut(qapp):
    dialog = SettingsDialog(AppSettings())
    assert dialog.processing_mode_combo.currentData() == "smart_cut"
    assert dialog.get_settings().default_processing_mode == "smart_cut"
    dialog.close()


@pytest.mark.parametrize("mode, reencode_controls", [
    ("smart_cut", True), ("simple_copy", False), ("full_reencode", True),
])
def test_encode_controls_follow_processing_mode(qapp, mode, reencode_controls):
    # 再エンコードのコーデック・走査方式・画質は元ソースで自動的に決まるため
    # 選択項目はない。再エンコードするモードでだけスレッド数とハードウェア
    # エンコーダーの判定結果を表示する。
    dialog = SettingsDialog(AppSettings())
    dialog.processing_mode_combo.setCurrentIndex(dialog.processing_mode_combo.findData(mode))

    assert _row_visible(dialog, dialog.threads_spin) is reencode_controls
    assert _row_visible(dialog, dialog._hardware_row) is reencode_controls
    assert dialog.processing_mode_description.text() != ""
    assert dialog.get_settings().default_processing_mode == mode
    for removed in ("crf_spin", "preset_edit", "gpu_enabled_check", "gpu_type_combo",
                    "full_reencode_codec_combo", "full_reencode_scan_combo"):
        assert not hasattr(dialog, removed)
    dialog.close()


def test_settings_dialog_shows_hardware_detection_and_redetects(qapp, monkeypatch):
    from ts_smartcut.ffmpeg.gpu import HardwareDetection
    from ts_smartcut.gui import settings_dialog as module

    calls = []

    def fake_get(settings, force=False):
        calls.append(force)
        return HardwareDetection()

    monkeypatch.setattr(module, "get_hardware_encoders", fake_get)
    dialog = SettingsDialog(AppSettings())
    assert "ソフトウェアエンコーダー" in dialog.hardware_label.text()

    dialog.redetect_button.click()
    assert calls == [False, True]
    dialog.close()


def test_unknown_saved_mode_falls_back_to_smart_cut(qapp):
    dialog = SettingsDialog(AppSettings(default_processing_mode="bogus"))
    assert dialog.processing_mode_combo.currentData() == "smart_cut"
    dialog.close()


def test_settings_dialog_rejects_invalid_filename_pattern(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr("ts_smartcut.gui.settings_dialog.QMessageBox.warning",
                        lambda parent, title, text: warnings.append(text))
    dialog = SettingsDialog(AppSettings())
    dialog.output_pattern_edit.setText("{name}_{foo}{ext}")
    dialog.accept()
    assert dialog.result() != dialog.DialogCode.Accepted and "{foo}" in warnings[0]

    dialog.output_pattern_edit.setText("{name}_{date}{ext}")
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.get_settings().output_filename_pattern == "{name}_{date}{ext}"


DEVICES = [("auto", "Autoselect device"), ("pulse/speaker", "スピーカー"), ("pulse/usb", "USBヘッドホン")]


def test_settings_dialog_selects_audio_device(qapp):
    dialog = SettingsDialog(AppSettings(), audio_devices=DEVICES)
    combo = dialog.audio_device_combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["auto", "pulse/speaker", "pulse/usb"]
    assert combo.currentData() == "auto"
    combo.setCurrentIndex(combo.findData("pulse/usb"))
    assert dialog.get_settings().audio_output_device == "pulse/usb"


def test_settings_dialog_keeps_missing_audio_device(qapp):
    # 外したUSB機器など、今は無い出力先も選択を残す(再生時は自動になる)
    dialog = SettingsDialog(AppSettings(audio_output_device="pulse/gone"), audio_devices=DEVICES)
    assert dialog.audio_device_combo.currentData() == "pulse/gone"
    assert "接続されていません" in dialog.audio_device_combo.currentText()
    assert dialog.get_settings().audio_output_device == "pulse/gone"


def test_settings_dialog_without_mpv_offers_auto(qapp):
    dialog = SettingsDialog(AppSettings(audio_output_device="default"))
    assert dialog.audio_device_combo.currentData() == "auto"


def test_hardware_detection_text_is_not_clipped(qapp):
    # ダイアログは開いた時のタブ(全般)の高さで表示され、折り返す説明文のある
    # エンコードタブに切り替えると、判定結果の行が上下で切れていた
    dialog = SettingsDialog(AppSettings())
    dialog.resize(740, 300)  # 長い音声デバイス名で横に広がった時(撮影時)と同じ幅
    dialog.show()
    qapp.processEvents()
    dialog.findChild(QTabWidget).setCurrentIndex(2)  # エンコード
    for _ in range(3):
        qapp.processEvents()
    label = dialog.hardware_label
    assert label.height() >= label.minimumSizeHint().height()
    dialog.close()


def test_settings_dialog_output_format_and_templates(qapp, monkeypatch, tmp_path):
    # 出力形式(設計書 §6.9): 選んだ形式のテンプレートの行だけ出し、誤ったテンプレートは保存しない
    warnings = []
    monkeypatch.setattr("ts_smartcut.gui.settings_dialog.QMessageBox.warning",
                        lambda parent, title, text: warnings.append(text))
    dialog = SettingsDialog(AppSettings())
    form = dialog._encode_form
    assert dialog.output_format_combo.currentData() == "ts"
    assert not form.isRowVisible(dialog._avs_template_row) and not form.isRowVisible(dialog._vpy_template_row)
    dialog.output_format_combo.setCurrentIndex(dialog.output_format_combo.findData("vpy"))
    assert form.isRowVisible(dialog._vpy_template_row) and not form.isRowVisible(dialog._avs_template_row)

    bad = tmp_path / "bad.vpy"
    bad.write_text("v = __TSSC_VIDEO__\n", encoding="utf-8")
    dialog.vpy_template_edit.setText(str(bad))
    dialog.accept()
    assert dialog.result() != dialog.DialogCode.Accepted and "__TSSC_AUDIO__" in warnings[0]

    good = tmp_path / "good.vpy"
    good.write_text("v = __TSSC_VIDEO__\na = __TSSC_AUDIO__\n__TSSC_TRIM__\n", encoding="utf-8")
    dialog.vpy_template_edit.setText(str(good))
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted
    settings = dialog.get_settings()
    assert settings.output_format == "vpy" and settings.vpy_template == good and settings.avs_template is None
