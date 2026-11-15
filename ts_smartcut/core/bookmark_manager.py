"""ブックマーク管理 (design doc §20)。

mpv/GUIには依存しない。ジャンプ操作自体は呼び出し側が
`preview_widget.seek_frame(bookmark.frame)` のように行う
（CutManager同様、コアロジックはmpv制御から分離する）。
"""

from __future__ import annotations

from typing import List, Optional

from ts_smartcut.models import Bookmark


class BookmarkManager:
    def __init__(self):
        self.bookmarks: List[Bookmark] = []

    def add_bookmark(self, frame: int, name: Optional[str] = None) -> Bookmark:
        """ブックマークを追加する（フレーム順にソートされる）。"""
        if name is None:
            name = f"Bookmark {len(self.bookmarks) + 1}"

        bookmark = Bookmark(frame, name)
        self.bookmarks.append(bookmark)
        self.bookmarks.sort(key=lambda b: b.frame)
        return bookmark

    def remove_bookmark(self, index: int) -> bool:
        """ブックマークを削除する。"""
        if not (0 <= index < len(self.bookmarks)):
            return False
        self.bookmarks.pop(index)
        return True

    def get_bookmark(self, index: int) -> Optional[Bookmark]:
        if not (0 <= index < len(self.bookmarks)):
            return None
        return self.bookmarks[index]

    def clear(self) -> None:
        self.bookmarks.clear()
