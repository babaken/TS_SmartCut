"""エンコード開始ボタン・バッチ追加ボタン(消音の隣、2026-10-06)。

エンコード開始はバッチリストを使わず、押した時点の残す区間で1件だけ処理する。
処理中は画面の操作を受け付けず(中止ボタンだけ押せる)、窓も閉じられない。
プレビュー・シークバーは処理中の位置へ動き、終わったらメッセージを出す。
"""

import subprocess
import threading

import pytest
from PySide6.QtWidgets import QMessageBox

from tests._qt_helpers import pump_events
from ts_smartcut.ffmpeg.gpu import HardwareDetection
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper

# windowフィクスチャ(module scope)はtests/conftest.pyで定義されている。


@pytest.fixture
def messages(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: shown.append(text))
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: shown.append(text))
    return shown


@pytest.fixture
def opened(window, interlaced_sample_video, tmp_path, monkeypatch):
    # 実際のGPUで試しエンコードする判定は時間がかかるのでソフトウェアで処理する
    monkeypatch.setattr(type(window), "_encode_hardware", HardwareDetection())
    window.settings.default_output_dir = tmp_path
    window.open_file(interlaced_sample_video)
    window.cut_manager.cuts.clear()
    window.batch_manager.items.clear()
    yield window
    window.settings.default_output_dir = None
    window.batch_manager.items.clear()


def _controls_order(window):
    layout = window.mute_button.parentWidget().layout()
    for i in range(layout.count()):
        sub = layout.itemAt(i).layout()
        if sub is not None and sub.indexOf(window.mute_button) >= 0:
            return [sub.itemAt(j).widget() for j in range(sub.count())]
    raise AssertionError("消音ボタンの並びが見つからない")


def test_buttons_sit_next_to_mute_in_order(window):
    widgets = _controls_order(window)
    i = widgets.index(window.mute_button)
    assert widgets[i + 1:i + 3] == [window.start_encode_button, window.add_to_batch_button]
    assert window.start_encode_button.text() == "エンコード開始"
    assert window.add_to_batch_button.text() == "バッチ追加"


def test_buttons_disabled_until_a_file_is_open(window):
    # (このモジュールの最初、まだファイルを開いていない状態)
    assert not window.start_encode_button.isEnabled()
    assert not window.add_to_batch_button.isEnabled()


def test_add_to_batch_button_adds_the_file(opened, messages):
    opened.cut_manager.add_cut(5, 40)
    opened.add_to_batch_button.click()
    assert len(opened.batch_manager.items) == 1
    assert opened.batch_manager.items[0].file_info.cuts[0].start_frame == 5


def test_start_encode_without_keep_ranges_is_refused(opened, messages):
    opened.start_encode_button.click()
    assert opened.encode_manager is None or not opened.is_encoding()
    assert messages and "残す区間がありません" in messages[0]


def test_encode_locks_the_window_follows_progress_and_reports_done(opened, messages, qapp, tmp_path, monkeypatch):
    seeks = []
    real_seek = opened.preview_widget.seek_frame
    monkeypatch.setattr(opened.preview_widget, "seek_frame", lambda frame: (seeks.append(frame), real_seek(frame)))
    opened.cut_manager.add_cut(5, 40)
    opened.cut_manager.add_cut(50, 70)

    opened.start_encode_button.click()
    assert opened.is_encoding()
    assert opened.encode_label.text().startswith("エンコード準備中")  # 最初の段階が始まるまで
    # 操作を受け付けない(中止ボタンだけ押せる)
    assert not opened.menuBar().isEnabled()
    assert not opened.centralWidget().isEnabled()
    assert not any(s.isEnabled() for s in opened._shortcuts)
    assert opened.cancel_encode_button.isVisible() and opened.cancel_encode_button.isEnabled()
    # 押した後に区間を変えても処理には影響しない
    opened.cut_manager.add_cut(72, 74)

    assert pump_events(qapp, lambda: messages, timeout_ms=60000)
    assert "完了しました" in messages[0], messages
    output = opened.encode_manager.items[0].output_path
    assert str(output) in messages[0] and output.exists()
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(output)],
        capture_output=True, text=True,
    ).stdout.strip()
    assert int(frames) == 35 + 20
    # プレビュー(とシークバー)は処理中の位置(残す区間の中)へ動いた
    assert seeks and all(5 <= f < 40 or 50 <= f < 70 for f in seeks)
    # mpvの移動が終わる前は位置のポーリングが途中の位置を書くことがあるので、揃うまで待つ
    assert pump_events(qapp, lambda: opened.seek_slider.value() == seeks[-1], timeout_ms=3000)
    # バッチリストには入れない。終わったら操作できる状態に戻る
    assert opened.batch_manager.items == []
    assert opened.menuBar().isEnabled() and opened.centralWidget().isEnabled()
    assert all(s.isEnabled() for s in opened._shortcuts)
    assert not opened.cancel_encode_button.isVisible()


def test_cannot_close_while_encoding_and_cancel_stops_it(opened, messages, qapp, monkeypatch):
    # 中止するまで終わらないffmpegの代わり
    started = threading.Event()

    def slow_run(self, args, progress_callback=None, bytes_per_frame=None):
        from ts_smartcut.errors import ProcessingCancelledError

        started.set()
        if self.cancel_event.wait(30):
            raise ProcessingCancelledError("E504", "中止しました")

    monkeypatch.setattr(FFmpegWrapper, "run", slow_run)
    opened.cut_manager.add_cut(5, 40)
    opened.start_encode_button.click()
    assert started.wait(10)

    opened.close()  # 閉じられない
    assert opened.isVisible()
    assert "閉じられません" in messages[-1]

    opened.cancel_encode_button.click()
    assert pump_events(qapp, lambda: not opened.is_encoding() and "中止しました" in messages[-1], timeout_ms=10000)
    assert opened.menuBar().isEnabled() and opened.centralWidget().isEnabled()
    assert not opened.cancel_encode_button.isVisible()


def test_encode_is_refused_while_the_batch_runs(opened, messages, monkeypatch):
    opened.cut_manager.add_cut(5, 40)
    monkeypatch.setattr(opened.batch_manager, "is_running", True)
    opened.start_encode_button.click()
    assert not opened.is_encoding()
    assert "バッチ処理の実行中" in messages[0]
