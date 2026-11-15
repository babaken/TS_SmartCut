"""音声波形の表示 (design doc §21)。プレビューの下・カット操作の上に置く。

今の位置を中央に、前後WINDOW_SECONDS秒の音声のピークを描く。再生やフレーム
送りに合わせて流れ、CMの境目の無音がフレーム単位で見える。残す区間の外
(出力から消える部分)は色を付け、クリックするとその位置へ移動する。
"""

from __future__ import annotations

import math
from typing import List, Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from ts_smartcut.core.waveform import AudioEnvelope
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.models import CutRange, removed_ranges

WINDOW_SECONDS = 10.0  # 中央(今の位置)から左右それぞれ何秒を表示するか
WAVEFORM_VIEW_HEIGHT = 64
# 高さはdBで表す(放送の音声は最大より十分小さく、そのままの比率では小さく見える)。
# FLOOR_DB以下は無音として高さ0。
FLOOR_DB = -50.0

_WAVE_COLOR = "#1f77b4"
_PLAYHEAD_COLOR = "#ff0000"


class WaveformView(QWidget):
    frame_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(WAVEFORM_VIEW_HEIGHT)
        self._envelope: Optional[AudioEnvelope] = None
        self._fps = 0.0
        self._current_frame = 0
        self._cuts: List[CutRange] = []
        self._proposals: list = []
        self._loading = False
        self._theme = {"timeline_bg": "#f0f0f0", "fg": "#000000", "remove_region": "#ff6b6b"}

    # ---- 状態 -------------------------------------------------------------

    def set_theme(self, theme: dict) -> None:
        self._theme = theme
        self.update()

    def set_loading(self, fps: float) -> None:
        """ファイルを開いた直後(波形を作っている間)。"""
        self._envelope = None
        self._fps = fps
        self._loading = True
        self.update()

    def set_envelope(self, envelope: Optional[AudioEnvelope], fps: float) -> None:
        self._envelope = envelope
        self._fps = fps
        self._loading = False
        self.update()

    def set_current_frame(self, frame: int) -> None:
        if frame != self._current_frame:
            self._current_frame = frame
            self.update()

    def set_proposals(self, ranges) -> None:
        """未判断の自動カットの案 [(開始, 終了)]。下側に青の帯で描く。"""
        self._proposals = list(ranges)
        self.update()

    def set_cuts(self, cuts: List[CutRange]) -> None:
        self._cuts = list(cuts)
        self.update()

    # ---- 座標 -------------------------------------------------------------

    def _seconds_per_pixel(self) -> float:
        return 2 * WINDOW_SECONDS / max(self.width(), 1)

    def _x_to_seconds(self, x: float) -> float:
        """x(画素)の位置の時刻(映像の先頭からの秒)。中央が今のフレーム。"""
        center = self._current_frame / self._fps if self._fps > 0 else 0.0
        return center + (x - self.width() / 2) * self._seconds_per_pixel()

    def _seconds_to_x(self, seconds: float) -> float:
        center = self._current_frame / self._fps if self._fps > 0 else 0.0
        return self.width() / 2 + (seconds - center) / self._seconds_per_pixel()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._fps > 0:
            frame = round(self._x_to_seconds(event.position().x()) * self._fps)
            self.frame_clicked.emit(max(frame, 0))
            event.accept()
            return
        super().mousePressEvent(event)

    # ---- 描画 -------------------------------------------------------------

    def column_levels(self) -> List[float]:
        """画素の列ごとの高さ(0〜1)。ピーク値をdBにし、FLOOR_DB〜0dBを0〜1にする。"""
        levels = []
        for peak in self.column_peaks():
            db = 20 * math.log10(max(peak, 1e-9))
            levels.append(min(max((db - FLOOR_DB) / -FLOOR_DB, 0.0), 1.0))
        return levels

    def column_peaks(self) -> List[float]:
        """画素の列ごとのピーク値(0〜1)。波形が無い所は0。"""
        width = self.width()
        envelope = self._envelope
        if envelope is None or len(envelope.peaks) == 0 or self._fps <= 0 or width <= 0:
            return [0.0] * max(width, 0)
        left, step = self._x_to_seconds(0), self._seconds_per_pixel()
        starts = [math.floor((left + k * step - envelope.offset) * envelope.rate) for k in range(width + 1)]
        count = len(envelope.peaks)
        result = [0.0] * width
        for x in range(width):
            lo, hi = max(starts[x], 0), min(max(starts[x + 1], starts[x] + 1), count)
            if lo < hi:
                result[x] = max(envelope.peaks[lo:hi]) / 32767.0
        return result

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(self._theme.get("timeline_bg", "#f0f0f0")))

        if self._fps > 0:
            # 残す区間の外(消す部分)。終わりは表示範囲の右端まであれば足りる。
            cut_color = QColor(self._theme.get("remove_region", "#ff6b6b"))
            cut_color.setAlpha(110)
            view_end = int((self._x_to_seconds(self.width()) + 1) * self._fps) + 1
            last = max([view_end] + [c.end_frame for c in self._cuts])
            # 区間が1つも無いうちは塗らない(開いた直後に全体が赤くなるため)
            for cut in removed_ranges(last, self._cuts) if self._cuts else []:
                x1 = max(self._seconds_to_x(cut.start_frame / self._fps), 0)
                x2 = min(self._seconds_to_x(cut.end_frame / self._fps), self.width())
                if x2 >= 0 and x1 <= self.width():
                    painter.fillRect(QRect(int(x1), 0, max(int(x2 - x1), 1), rect.height()), cut_color)

        if self._fps > 0 and self._proposals:
            from ts_smartcut.gui.timeline import PROPOSAL_COLOR

            band = max(rect.height() // 6, 3)
            for start, end in self._proposals:
                x1 = max(self._seconds_to_x(start / self._fps), 0)
                x2 = min(self._seconds_to_x(end / self._fps), self.width())
                if x2 >= 0 and x1 <= self.width():
                    painter.fillRect(QRect(int(x1), rect.height() - band, max(int(x2 - x1), 1), band),
                                     QColor(PROPOSAL_COLOR))

        mid_y = rect.height() // 2
        if self._loading:
            painter.setPen(QColor(self._theme.get("fg", "#000000")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, tr("waveform.making"))
        elif self._envelope is not None:
            painter.setPen(QColor(_WAVE_COLOR))
            half = rect.height() // 2 - 2
            for x, level in enumerate(self.column_levels()):
                bar = int(level * half)
                painter.drawLine(x, mid_y - bar, x, mid_y + bar)

        # 今のフレーム(中央)
        painter.setPen(QColor(_PLAYHEAD_COLOR))
        center_x = self.width() // 2
        painter.drawLine(center_x, 0, center_x, rect.height())
        painter.setPen(QColor(self._theme.get("fg", "#000000")))
        painter.drawText(QRect(4, 2, 80, 14), Qt.AlignmentFlag.AlignLeft,
                         tr("waveform.seconds", value=f"-{WINDOW_SECONDS:.0f}"))
        painter.drawText(QRect(self.width() - 84, 2, 80, 14), Qt.AlignmentFlag.AlignRight,
                         tr("waveform.seconds", value=f"+{WINDOW_SECONDS:.0f}"))
        painter.end()
