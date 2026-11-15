"""アプリケーション設定の永続化 (design doc §23)。

シリアライズ自体はmodels.AppSettingsのto_dict/from_dict（Phase1）を
再利用する。project_manager.pyと同様のファイルI/O・エラー処理方針。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from ts_smartcut.models import AppSettings
from ts_smartcut.system.app_dir import app_base_dir


# 設定・記録 DB・ログのフォルダをこのパスに変える環境変数(テストとスクリーンショットの
# ツールが利用者の設定を汚さないため)。
CONFIG_DIR_ENV = "TS_SMARTCUT_CONFIG_DIR"


def get_config_dir() -> Path:
    """設定・記録 DB・ログを置くフォルダ。CONFIG_DIR_ENV があればそこ、無ければ実行ファイルと
    同じフォルダ(ソースから動かす時はリポジトリの直下。2026-10-12 に OS 標準の設定フォルダ
    から変更、設計書 §31.9。前の場所のファイルは起動時に main.py が移す)。"""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    return app_base_dir()


def get_default_settings_path() -> Path:
    """設定のフォルダ(get_config_dir)に settings.json を置く。"""
    return get_config_dir() / "settings.json"


class SettingsManager:
    @staticmethod
    def save(settings: AppSettings, path: Optional[Path] = None) -> Path:
        """設定をJSONファイルへ保存する。保存先パスを返す。"""
        path = path or get_default_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(settings.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    @staticmethod
    def load(path: Optional[Path] = None) -> AppSettings:
        """設定をJSONファイルから読み込む。ファイルが存在しない、または
        壊れている場合はデフォルト設定にフォールバックする
        （設定ファイルの破損でアプリが起動不能になることを避けるため）。"""
        path = path or get_default_settings_path()
        if not path.exists():
            return AppSettings()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return AppSettings.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            return AppSettings()
