"""プロジェクトの保存・読込 (design doc §10)。

シリアライズ自体はmodels.Projectのto_dict/from_dictが担う
（Path/datetimeの変換込み）。ここではファイルI/Oと版の確かめを行う。
8.0〜8.2 の変換は 2026-11-19 に外した(公開前の版のファイルだけ。公開した版はどれも 8.3 を書く)。"""

from __future__ import annotations

import json
from pathlib import Path

from ts_smartcut.errors import ProjectError
from ts_smartcut.models import Project

CURRENT_PROJECT_VERSION = "8.3"


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
        """JSONファイルからプロジェクトを読み込む。版が CURRENT_PROJECT_VERSION でなければ E403。"""
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
                raise ProjectError("E403", key="exceptions.project_version", version=data.get("version"))
            return Project.from_dict(data)
        except (KeyError, ValueError, TypeError) as e:
            raise ProjectError("E401", key="exceptions.project_invalid", error=e)
