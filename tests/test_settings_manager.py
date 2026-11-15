from pathlib import Path

from ts_smartcut.core.settings_manager import (
    CONFIG_DIR_ENV,
    SettingsManager,
    get_config_dir,
    get_default_settings_path,
)
from ts_smartcut.models import AppSettings


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings(theme="dark", cpu_threads=4, default_processing_mode="simple_copy")

    SettingsManager.save(settings, path)
    loaded = SettingsManager.load(path)

    assert loaded == settings


def test_load_ignores_settings_from_older_versions(tmp_path):
    # 以前の版にあった項目(画質・GPUの手動設定等)が残っていても起動できる
    path = tmp_path / "settings.json"
    path.write_text('{"theme": "dark", "crf_value": 23, "gpu_enabled": true, "preset": "slow"}', encoding="utf-8")
    loaded = SettingsManager.load(path)
    assert loaded.theme == "dark"


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "settings.json"
    SettingsManager.save(AppSettings(), path)
    assert path.exists()


def test_load_missing_file_returns_defaults(tmp_path):
    loaded = SettingsManager.load(tmp_path / "does_not_exist.json")
    assert loaded == AppSettings()


def test_load_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")

    loaded = SettingsManager.load(path)

    assert loaded == AppSettings()


def test_load_json_missing_required_structure_returns_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"unexpected": "shape"}', encoding="utf-8")

    loaded = SettingsManager.load(path)

    assert loaded == AppSettings()


def test_save_round_trips_path_fields(tmp_path):
    path = tmp_path / "settings.json"
    settings = AppSettings(
        temp_directory=Path("/tmp/ts_smartcut"),
        default_output_dir=Path("/videos/out"),
    )

    SettingsManager.save(settings, path)
    loaded = SettingsManager.load(path)

    assert loaded.temp_directory == Path("/tmp/ts_smartcut")
    assert loaded.default_output_dir == Path("/videos/out")


def test_get_default_settings_path_is_platform_appropriate():
    path = get_default_settings_path()
    assert path.name == "settings.json"
    assert "ts_smartcut" in path.parts


def test_config_dir_env_moves_settings_logs_and_history_db(tmp_path, monkeypatch):
    """設定・ログ・記録 DB・最近使ったファイル・GPU の判定結果は、どれも CONFIG_DIR_ENV の
    フォルダに置かれる(テストが利用者の設定を汚さない仕組み。既定の場所を変えても効く)。"""
    from ts_smartcut.autocut.history_db import get_default_history_db_path
    from ts_smartcut.core.hardware_encoders import detection_path
    from ts_smartcut.core.recent_files_manager import get_default_recent_files_path
    from ts_smartcut.logging_setup import get_default_log_path

    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "conf"))
    base = tmp_path / "conf"
    assert get_config_dir() == base
    assert get_default_settings_path() == base / "settings.json"
    assert get_default_log_path() == base / "logs" / "ts_smartcut.log"
    assert get_default_history_db_path() == base / "cut_history.db"
    assert get_default_recent_files_path() == base / "recent_files.json"
    assert detection_path() == base / "hardware_encoders.json"
