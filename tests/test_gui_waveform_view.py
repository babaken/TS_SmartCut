"""音声波形の表示(gui/waveform_view.py)。今の位置を中央に前後10秒を描く。"""

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import QEvent

from ts_smartcut.core.waveform import AudioEnvelope
from ts_smartcut.gui.waveform_view import WINDOW_SECONDS, WaveformView
from ts_smartcut.models import CutRange

FPS = 30.0


def _view(qapp, peaks, offset=0.0):
    view = WaveformView()
    view.resize(400, 64)
    view.set_envelope(AudioEnvelope(peaks=np.array(peaks, dtype=np.int16), rate=100, offset=offset), FPS)
    return view


def test_current_position_is_at_the_center(qapp):
    # 音は映像の先頭から20.0〜20.5秒だけ。今の位置を20秒にすると中央から右に音がある
    peaks = np.zeros(6000, dtype=np.int16)
    peaks[2000:2050] = 30000
    view = _view(qapp, peaks)
    view.set_current_frame(int(20 * FPS))
    columns = np.asarray(view.column_peaks())
    seconds_per_px = 2 * WINDOW_SECONDS / 400
    loud = np.nonzero(columns > 0.5)[0]
    assert loud.min() == 200  # 中央 = 今のフレーム
    assert abs((loud.max() + 1 - 200) * seconds_per_px - 0.5) < 2 * seconds_per_px
    view.repaint()


def test_audio_offset_from_the_video_start_is_applied(qapp):
    # 音声が映像より0.5秒先に始まる: 音声の先頭から20.5秒の音 = 映像の先頭から20秒
    peaks = np.zeros(6000, dtype=np.int16)
    peaks[2050:2100] = 30000
    view = _view(qapp, peaks, offset=-0.5)
    view.set_current_frame(int(20 * FPS))
    assert np.nonzero(np.asarray(view.column_peaks()) > 0.5)[0].min() == 200


def test_outside_the_recording_is_blank(qapp):
    view = _view(qapp, np.full(2000, 30000))  # 20秒
    view.set_current_frame(0)  # 左半分は録画の先頭より前
    columns = np.asarray(view.column_peaks())
    assert columns[:199].max() == 0 and columns[201:].min() > 0.5


def test_click_moves_to_that_frame(qapp):
    view = _view(qapp, np.zeros(6000))
    view.set_current_frame(600)  # 20秒
    clicked = []
    view.frame_clicked.connect(clicked.append)
    # 右端 = 今の位置 + 10秒
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(400, 30), QPointF(400, 30),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(event)
    assert clicked == [900]


def test_loading_and_cut_regions_paint(qapp):
    view = WaveformView()
    view.resize(400, 64)
    view.set_loading(FPS)
    view.repaint()
    view.set_envelope(None, FPS)  # 音声なし・作れなかった
    view.set_cuts([CutRange(0, 30)])
    view.repaint()


def test_heights_are_in_decibels(qapp):
    # -6dB(半分)は高さ約0.88、-40dBは約0.2、無音は0
    view = _view(qapp, [16384] * 100 + [328] * 100 + [0] * 100)
    view.set_current_frame(int(10 * FPS))  # 中央=10秒。左端=0秒
    levels = view.column_levels()
    px_per_second = 400 / (2 * WINDOW_SECONDS)
    at = lambda sec: levels[int(sec * px_per_second) + 2]
    assert abs(at(0.2) - (1 - 6.02 / 50)) < 0.02
    assert abs(at(1.2) - (1 - 40 / 50)) < 0.02
    assert at(2.2) == 0


def test_only_outside_the_keep_ranges_is_shaded(qapp):
    # 左端=0秒、右端=20秒。残す区間は5〜10秒(150〜300フレーム)
    view = WaveformView()
    view.resize(400, 64)
    view.set_envelope(None, FPS)
    view.set_current_frame(int(10 * FPS))
    background = view.grab().toImage().pixelColor(50, 5)
    view.set_cuts([CutRange(150, 300)])
    image = view.grab().toImage()
    assert image.pixelColor(150, 5) == background  # 7.5秒: 残す
    assert image.pixelColor(50, 5) != background   # 2.5秒: 消える
    assert image.pixelColor(350, 5) != background  # 17.5秒: 消える
