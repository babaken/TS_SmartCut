from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from ts_smartcut.gui.timeline import TimelineWidget
from ts_smartcut.models import Bookmark, CutRange


def _grab(widget):
    """論理座標で色を読める画像。拡大率 200% の画面では grab() が 2 倍の大きさになる。"""
    return widget.grab().toImage().scaled(widget.size())


def test_frame_to_x_and_back_roundtrip(qapp):
    tl = TimelineWidget()
    tl.resize(1000, 60)
    tl.set_total_frames(1000)

    for frame in (0, 250, 500, 999):
        x = tl._frame_to_x(frame)
        back = tl._x_to_frame(x)
        assert abs(back - frame) <= 1  # 整数丸めの範囲で一致


def test_x_to_frame_clamped_to_valid_range(qapp):
    tl = TimelineWidget()
    tl.resize(500, 60)
    tl.set_total_frames(100)

    assert tl._x_to_frame(-10) == 0
    assert tl._x_to_frame(10_000) == 99


def test_x_to_frame_zero_total_frames(qapp):
    tl = TimelineWidget()
    tl.resize(500, 60)
    assert tl._x_to_frame(100) == 0


def test_setters_do_not_raise(qapp):
    tl = TimelineWidget()
    tl.resize(500, 60)
    tl.set_total_frames(1000)
    tl.set_cuts([CutRange(100, 200), CutRange(500, 600)])
    tl.set_selected_cut_index(0)
    tl.set_warning_cut_indices({1})
    tl.set_bookmarks([Bookmark(300, "BM")])
    tl.set_keyframes([0, 100, 200, 300])
    tl.set_current_frame(400)
    tl.repaint()


def test_paint_with_zero_total_frames_does_not_raise(qapp):
    tl = TimelineWidget()
    tl.resize(500, 60)
    tl.repaint()  # total_frames=0 の初期状態


def test_mouse_click_emits_frame_clicked(qapp):
    tl = TimelineWidget()
    tl.resize(1000, 60)
    tl.set_total_frames(1000)

    received = []
    tl.frame_clicked.connect(received.append)

    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(500, 30),
        QPointF(500, 30),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tl.mousePressEvent(event)

    assert len(received) == 1
    assert 480 <= received[0] <= 520


def test_keep_ranges_are_green_and_the_rest_red(qapp):
    # set_cutsは残す区間(緑)。それ以外(出力から消える部分)は赤 (2026-10-06)
    from PySide6.QtGui import QColor

    from ts_smartcut.gui.theme import THEMES

    tl = TimelineWidget()
    tl.resize(1000, 60)
    tl.set_total_frames(1000)
    image = _grab(tl)
    assert image.pixelColor(150, 30) != QColor(THEMES["light"]["remove_region"])  # 区間なしは赤くしない

    tl.set_cuts([CutRange(100, 200)])
    image = _grab(tl)
    assert image.pixelColor(150, 30) == QColor(THEMES["light"]["keep_region"])
    assert image.pixelColor(500, 30) == QColor(THEMES["light"]["remove_region"])
