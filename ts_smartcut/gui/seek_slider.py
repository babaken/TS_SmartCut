"""The seek bar under the preview (design doc §4.2).

A plain QSlider moves one page towards a click on the groove; this one jumps the handle to the
clicked point and starts dragging it there, so the click seeks on release like a drag does."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider


class SeekSlider(QSlider):
    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)

    def value_at(self, x: float) -> int:
        """The value whose handle centre sits at x (widget coordinates)."""
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                      QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                      QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        position = round(x - groove.x() - handle.width() / 2)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                              position, max(span, 0), option.upsideDown)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            hit = self.style().hitTestComplexControl(QStyle.ComplexControl.CC_Slider, option,
                                                     event.position().toPoint(), self)
            if hit != QStyle.SubControl.SC_SliderHandle:
                # Signals blocked: valueChanged is not used for seeking, and the press below
                # (now on the handle) emits sliderPressed; the release then seeks.
                self.blockSignals(True)
                self.setValue(self.value_at(event.position().x()))
                self.blockSignals(False)
        super().mousePressEvent(event)
