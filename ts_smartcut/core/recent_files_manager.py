"""最近使用したファイルの履歴 (design doc §4.2, §10.4)。

動画ファイルとプロジェクトファイルを1つのリストで管理する。履歴は
settings.jsonではなく同じディレクトリのrecent_files.jsonに保存する
（AppSettingsはプロジェクトファイルにも埋め込まれるため、そこへ履歴を
含めると無関係なファイルパスがプロジェクトに漏れてしまう）。

履歴は補助的な機能なので、読み書きに失敗してもアプリの動作は止めない
（警告ログのみ）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from ts_smartcut.core.settings_manager import get_default_settings_path

logger = logging.getLogger(__name__)

DEFAULT_MAX_COUNT = 10


def get_default_recent_files_path() -> Path:
    return get_default_settings_path().parent / "recent_files.json"


class RecentFilesManager:
    def __init__(self, path: Optional[Path] = None, max_count: int = DEFAULT_MAX_COUNT):
        self.path = path or get_default_recent_files_path()
        self.max_count = max_count
        self.files: List[Path] = self._load()

    def _load(self) -> List[Path]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [Path(p) for p in data if isinstance(p, str)][: self.max_count]
        except (json.JSONDecodeError, TypeError, OSError) as e:
            logger.warning("Could not read the recent files list: %s: %s", self.path, e)
            return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([str(p) for p in self.files], indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Could not save the recent files list: %s: %s", self.path, e)

    def add(self, path: Path) -> None:
        """履歴の先頭へ追加する（既にあれば先頭へ移動）。"""
        path = Path(path).absolute()
        self.files = [path] + [p for p in self.files if p != path]
        del self.files[self.max_count :]
        self._save()

    def remove(self, path: Path) -> None:
        path = Path(path).absolute()
        if path in self.files:
            self.files.remove(path)
            self._save()

    def remove_missing(self) -> None:
        """存在しなくなったファイルを履歴から取り除く (design doc §10.4)。"""
        existing = [p for p in self.files if p.exists()]
        if existing != self.files:
            self.files = existing
            self._save()

    def set_max_count(self, max_count: int) -> None:
        self.max_count = max_count
        if len(self.files) > max_count:
            del self.files[max_count:]
            self._save()

    def clear(self) -> None:
        self.files = []
        self._save()
