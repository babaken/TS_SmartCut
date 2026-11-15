"""プレビューの下のシークバー (gui/seek_slider.py、設計書 §4.2)。"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from ts_smartcut.gui.seek_slider import SeekSlider


def _slider(qapp):
    slider = SeekSlider()
    slider.setRange(0, 1000)
    slider.resize(500, 30)
    slider.show()
    qapp.processEvents()
    return slider


def test_a_groove_click_jumps_there_and_ends_like_a_drag(qapp):
    slider = _slider(qapp)
    events = []
    slider.sliderPressed.connect(lambda: events.append(("pressed", slider.value())))
    slider.sliderReleased.connect(lambda: events.append(("released", slider.value())))
    for target in (750, 100, 1000, 0):
        events.clear()
        x = next(x for x in range(slider.width()) if slider.value_at(x) >= target)
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(x, 15))
        assert abs(slider.value() - target) <= 5  # 1 ピクセルは約 2 フレーム
        assert [e[0] for e in events] == ["pressed", "released"]
        assert events[-1][1] == slider.value()
    slider.close()


def test_value_at_covers_the_range(qapp):
    slider = _slider(qapp)
    assert slider.value_at(-50) == 0
    assert slider.value_at(10_000) == 1000
    values = [slider.value_at(x) for x in range(slider.width())]
    assert values == sorted(values)
    slider.close()


def test_a_disabled_slider_ignores_clicks(qapp):
    slider = _slider(qapp)
    slider.setValue(10)
    slider.setEnabled(False)
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(400, 15))
    assert slider.value() == 10
    slider.close()
