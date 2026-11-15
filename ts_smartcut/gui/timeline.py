"""残す区間タイムライン (design doc §4.7)。

QPainterによる直接描画。色分け:
緑=残す区間 / 赤=出力から消える部分 / 黄=選択中の区間 / 紫=ブックマーク位置 /
白点=キーフレーム位置。オレンジ(重複/警告)はCutManagerが重複を許さない
ため通常発生しないが、将来の警告表示用にAPIとして残す
（design doc §4.7）。クリックでその位置へシークする。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from ts_smartcut.gui.theme import THEMES
from ts_smartcut.models import Bookmark, CutRange

_PLAYHEAD_COLOR = "#ffffff"
_KEYFRAME_COLOR = "#ffffff"
_BOOKMARK_COLOR = "#8000ff"
_WARNING_COLOR = "#ffa500"
# 自動カットの「残す区間の案」(未判断、設計書 §31.6・§4.7)。最初はオレンジだったが、
# 切る部分の赤に重なって見えにくかったので青にした(2026-10-09 利用者の決定)
PROPOSAL_COLOR = "#1e88e5"


class TimelineWidget(QWidget):
    frame_clicked = Signal(int)
    cut_context_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(60)
        self._total_frames = 0
        self._cuts: List[CutRange] = []
        self._selected_cut_index: Optional[int] = None
        self._warning_cut_indices: set[int] = set()
        self._bookmarks: List[Bookmark] = []
        self._keyframes: List[int] = []
        self._proposals: List[tuple] = []
        self._current_frame = 0
        self._theme = THEMES["light"]

    # ---- 状態更新 -------------------------------------------------------

    def set_theme(self, theme: dict) -> None:
        self._theme = theme
        self.update()

    def set_total_frames(self, total_frames: int) -> None:
        self._total_frames = max(total_frames, 0)
        self.update()

    def set_cuts(self, cuts: List[CutRange]) -> None:
        self._cuts = list(cuts)
        self.update()

    def set_proposals(self, ranges: List[tuple]) -> None:
        """未判断の案 [(開始, 終了)]。トラックの下側に青の帯で描く。"""
        self._proposals = list(ranges)
        self.update()

    def set_selected_cut_index(self, index: Optional[int]) -> None:
        self._selected_cut_index = index
        self.update()

    def set_warning_cut_indices(self, indices: set[int]) -> None:
        self._warning_cut_indices = set(indices)
        self.update()

    def set_bookmarks(self, bookmarks: List[Bookmark]) -> None:
        self._bookmarks = list(bookmarks)
        self.update()

    def set_keyframes(self, keyframes: List[int]) -> None:
        self._keyframes = list(keyframes)
        self.update()

    def set_current_frame(self, frame: int) -> None:
        self._current_frame = frame
        self.update()

    def _frame_to_x(self, frame: int) -> int:
        if self._total_frames <= 0:
            return 0
        ratio = frame / self._total_frames
        return int(ratio * self.width())

    def _x_to_frame(self, x: int) -> int:
        if self.width() <= 0 or self._total_frames <= 0:
            return 0
        ratio = max(0.0, min(1.0, x / self.width()))
        return min(int(ratio * self._total_frames), max(self._total_frames - 1, 0))

    # ---- 描画 -----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        rect = self.rect()
        painter.fillRect(rect, QColor(self._theme["timeline_bg"]))

        if self._total_frames <= 0:
            painter.end()
            return

        track_rect = QRect(0, 10, self.width(), self.height() - 20)

        # 消える部分(赤)を全体のベースとして描き、残す区間(緑)を上書きする。
        # 区間が1つも無いうちは赤くしない(開いた直後に全体が赤くなるため)。
        if self._cuts:
            painter.fillRect(track_rect, QColor(self._theme["remove_region"]))

        for i, cut in enumerate(self._cuts):
            x1 = self._frame_to_x(cut.start_frame)
            x2 = self._frame_to_x(cut.end_frame)
            color_hex = self._theme["keep_region"]
            if i in self._warning_cut_indices:
                color_hex = _WARNING_COLOR
            color = QColor(color_hex)
            if i == self._selected_cut_index:
                color = QColor("#ffff00")  # 選択中の区間は黄色
            painter.fillRect(QRect(x1, track_rect.top(), max(x2 - x1, 1), track_rect.height()), color)
            if cut.is_excluded:
                painter.save()
                painter.setClipRect(QRect(x1, track_rect.top(), max(x2 - x1, 1), track_rect.height()))
                painter.setPen(QColor("#245b24"))
                for hatch_x in range(x1 - track_rect.height(), x2, 8):
                    painter.drawLine(hatch_x, track_rect.bottom(), hatch_x + track_rect.height(), track_rect.top())
                painter.restore()

        # 自動カットの案(未判断): トラックの下側 4 割に青の帯
        if self._proposals:
            band = max(track_rect.height() * 2 // 5, 4)
            proposal_color = QColor(PROPOSAL_COLOR)
            for start, end in self._proposals:
                x1 = self._frame_to_x(start)
                x2 = self._frame_to_x(end)
                painter.fillRect(QRect(x1, track_rect.bottom() - band + 1, max(x2 - x1, 1), band), proposal_color)

        # キーフレーム位置(白点、視認性のため小さな矩形で描く)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_KEYFRAME_COLOR))
        for kf in self._keyframes:
            x = self._frame_to_x(kf)
            painter.drawRect(QRect(x - 1, track_rect.bottom() - 3, 2, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # ブックマーク位置(紫の縦線)
        painter.setPen(QColor(_BOOKMARK_COLOR))
        for bookmark in self._bookmarks:
            x = self._frame_to_x(bookmark.frame)
            painter.drawLine(x, track_rect.top(), x, track_rect.bottom())

        # 現在再生位置(プレイヘッド)
        painter.setPen(QColor(_PLAYHEAD_COLOR))
        x = self._frame_to_x(self._current_frame)
        painter.drawLine(x, 0, x, self.height())

        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            frame = self._x_to_frame(int(event.position().x()))
            self.frame_clicked.emit(frame)
        elif event.button() == Qt.MouseButton.RightButton:
            frame = self._x_to_frame(int(event.position().x()))
            for index, cut in enumerate(self._cuts):
                if cut.start_frame <= frame < cut.end_frame:
                    self.cut_context_requested.emit(index)
                    break
