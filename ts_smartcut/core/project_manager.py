"""プロジェクトの保存・読込 (design doc §10)。

シリアライズ自体はmodels.Projectのto_dict/from_dictが担う
（Path/datetimeの変換込み）。ここではファイルI/Oとバージョン
マイグレーションの枠組みを提供する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict

from ts_smartcut.errors import ProjectError
from ts_smartcut.models import Project

CURRENT_PROJECT_VERSION = "8.3"


def _migrate_8_0(data: dict) -> dict:
    """8.0のcutsは削除する区間だった。8.1から残す区間なので補集合に変える
    (2026-10-06)。"""
    from ts_smartcut.models import CutRange, removed_ranges

    data = dict(data)
    file_info = dict(data["file_info"])
    removed = [CutRange.from_dict(c) for c in file_info.get("cuts", [])]
    file_info["cuts"] = [c.to_dict() for c in removed_ranges(file_info["total_frames"], removed)]
    data["file_info"] = file_info
    data["version"] = CURRENT_PROJECT_VERSION
    return data


# 旧バージョン -> 変換関数 のマップ。未知のバージョンはE403で明確に拒否する
# （design doc の `pass` プレースホルダーのようにサイレントに無視しない）。
def _migrate_8_1(data: dict) -> dict:
    """8.1 projects predate persistent edit history and excluded ranges."""
    data = dict(data)
    data["edit_events"] = []
    file_info = dict(data["file_info"])
    file_info["cuts"] = [{**cut, "is_excluded": False} for cut in file_info.get("cuts", [])]
    data["file_info"] = file_info
    data["version"] = CURRENT_PROJECT_VERSION
    return data


def _migrate_8_2(data: dict) -> dict:
    """8.2 までは音声を選べなかった(最初の音声。設計書 §4.12、2026-10-26)。中身は変えない。"""
    data = dict(data)
    data["version"] = CURRENT_PROJECT_VERSION
    return data


_MIGRATIONS: Dict[str, Callable[[dict], dict]] = {
    "8.0": _migrate_8_0, "8.1": _migrate_8_1, "8.2": _migrate_8_2,
}


class ProjectManager:
    @staticmethod
    def save_project(project: Project, path: Path) -> None:
        """プロジェクトをJSONファイルへ保存する。"""
        data = project.to_dict()
        try:
            Path(path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            raise ProjectError("E401", key="exceptions.project_write_failed", error=e)

    @staticmethod
    def load_project(path: Path) -> Project:
        """JSONファイルからプロジェクトを読み込む。バージョンが異なる
        場合は既知の移行元であればマイグレーションし、未知であれば
        E403を送出する。"""
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise ProjectError("E401", key="exceptions.project_read_failed", error=e)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProjectError("E401", key="exceptions.project_invalid_json", error=e)

        try:
            if data.get("version") != CURRENT_PROJECT_VERSION:
                data = ProjectManager._migrate(data)
            return Project.from_dict(data)
        except (KeyError, ValueError, TypeError) as e:
            raise ProjectError("E401", key="exceptions.project_invalid", error=e)

    @staticmethod
    def _migrate(data: dict) -> dict:
        version = data.get("version")
        migrate_fn = _MIGRATIONS.get(version)
        if migrate_fn is None:
            raise ProjectError("E403", key="exceptions.project_version", version=version)
        return migrate_fn(data)
