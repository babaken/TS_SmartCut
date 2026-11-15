"""音声の選択 (設計書 §4.12、2026-10-26 利用者の決定)。

録画に音声が 2 つ以上ある時、開くたびに出す(モーダル)。音声ごとの行をラジオボタンで選び、「聞く位置」の
スライダーと再生／停止で聞き比べる。音はプレビューの mpv で鳴らす(mpv を 2 つ動かすと落ちやすいので、
もう 1 つは作らない。裏のプレビューの映像も一緒に動く)。再生中に音声を切り替えると同じ位置のまま
変わる。OK で決め、キャンセル・× は 1 つ目の音声。再生を止める・プレビューの位置と消音を戻すのは
呼び出し側(MainWindow)。
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
)

from ts_smartcut.broadcast.audio_tracks import AudioTrack
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.time_utils import frame_to_time

POLL_MS = 200


class AudioPreview(Protocol):
    """選択の画面が使うプレビューの操作(MainWindow が用意する)。"""

    def seek_frame(self, frame: int) -> None: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def set_audio_pid(self, pid: Optional[int]) -> None: ...
    def current_frame(self) -> Optional[int]: ...


def _hms(frame: int, fps: float) -> str:
    seconds = int(frame_to_time(max(frame, 0), fps)) if fps > 0 else 0
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class AudioSelectDialog(QDialog):
    def __init__(self, tracks: List[AudioTrack], total_frames: int, fps: float, start_frame: int,
                 preview: AudioPreview, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("audio.select_title"))
        self.setModal(True)
        self._tracks = list(tracks)
        self._fps = fps
        self._preview = preview
        self._playing = False
        self._dragging = False

        layout = QVBoxLayout(self)
        intro = QLabel(tr("audio.select_intro", count=len(self._tracks)))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._group = QButtonGroup(self)
        self.radios: List[QRadioButton] = []
        for i, track in enumerate(self._tracks):
            radio = QRadioButton(track.label())
            self._group.addButton(radio, i)
            self.radios.append(radio)
            layout.addWidget(radio)
        self.radios[0].setChecked(True)
        self._group.idToggled.connect(self._on_toggled)

        layout.addSpacing(8)
        layout.addWidget(QLabel(tr("audio.listen_position")))
        row = QHBoxLayout()
        self.play_button = QPushButton(tr("audio.play"))
        self.play_button.clicked.connect(self.play)
        row.addWidget(self.play_button)
        self.stop_button = QPushButton(tr("audio.stop"))
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        row.addWidget(self.stop_button)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(total_frames - 1, 0))
        self.slider.setValue(min(max(start_frame, 0), self.slider.maximum()))
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.valueChanged.connect(self._on_slider_value)
        row.addWidget(self.slider, 1)
        self.time_label = QLabel(_hms(self.slider.value(), fps))
        row.addWidget(self.time_label)
        layout.addLayout(row)

        for key in ("audio.note_compare", "audio.note_proposals"):
            note = QLabel(tr(key))
            note.setWordWrap(True)
            layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(560)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        self._preview.set_audio_pid(self._tracks[0].pid)
        self._preview.seek_frame(self.slider.value())

    # ---- 選んだ音声 --------------------------------------------------------------

    def chosen_index(self) -> int:
        return max(self._group.checkedId(), 0)

    def selected_track(self) -> AudioTrack:
        """OK なら選んだ音声、キャンセル・× なら 1 つ目の音声。"""
        if self.result() == QDialog.DialogCode.Accepted:
            return self._tracks[self.chosen_index()]
        return self._tracks[0]

    def _on_toggled(self, index: int, checked: bool) -> None:
        if checked:
            self._preview.set_audio_pid(self._tracks[index].pid)

    # ---- 聞く位置と再生 --------------------------------------------------------------

    def play(self) -> None:
        self._playing = True
        self._preview.play()
        self.play_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._timer.start()

    def stop(self) -> None:
        self._playing = False
        self._timer.stop()
        self._preview.pause()
        self.play_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _on_slider_value(self, frame: int) -> None:
        self.time_label.setText(_hms(frame, self._fps))
        if not self._dragging and not self.slider.signalsBlocked():
            self._preview.seek_frame(frame)

    def _on_slider_released(self) -> None:
        self._dragging = False
        self._preview.seek_frame(self.slider.value())

    def _poll(self) -> None:
        if self._dragging:
            return
        frame = self._preview.current_frame()
        if frame is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(min(frame, self.slider.maximum()))
            self.slider.blockSignals(False)
            self.time_label.setText(_hms(frame, self._fps))

    def done(self, result: int) -> None:
        if self._playing:
            self.stop()
        super().done(result)
