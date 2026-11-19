"""Phase 6b: mpv render API統合の検証。

Phase 6aの最小QOpenGLWidgetをテストハーネストとして使い、実mpv/libmpvで
実際にフレーム描画・シーク・速度変更・コマンド実行ができることを検証
する。実mpvインスタンスの生成はプロセス内で1つに使い回す
（CLAUDE.mdのテスト方針: 複数回生成すると旧wid方式で実際にクラッシュが
発生した経緯があるため）。
"""

from ctypes import CFUNCTYPE, c_char_p, c_void_p

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QMainWindow

from tests._qt_helpers import pump_events
from ts_smartcut.errors import MPVIPCError
from ts_smartcut.mpv import create_controller, is_mpv_available
from ts_smartcut.mpv.controller import MPVController


class _UpdateSignal(QObject):
    frame_ready = Signal()


class _PreviewTestWidget(QOpenGLWidget):
    """本番のgui/preview_widget.py(Phase 7)を先取りした最小実装。
    mpvのupdate_cbはmpv内部スレッドから呼ばれるため、Qtシグナル
    （キュー接続）経由でGUIスレッドの再描画へブリッジする。"""

    def __init__(self):
        super().__init__()
        self.controller = None
        self.frame_ready_count = 0
        self._update_signal = _UpdateSignal()
        self._update_signal.frame_ready.connect(self.update)

    def initializeGL(self):
        def get_proc_address(name: bytes) -> int:
            return self.context().getProcAddress(name) or 0

        # ao=null: tests/conftest.pyのmpv_without_audio_outputと同じ理由
        self.controller = create_controller(get_proc_address, ao="null")
        if self.controller is not None:
            self.controller.set_update_callback(self._on_mpv_update)

    def _on_mpv_update(self):
        self.frame_ready_count += 1
        self._update_signal.frame_ready.emit()

    def paintGL(self):
        if self.controller is None:
            return
        fbo = self.defaultFramebufferObject()
        w = int(self.width() * self.devicePixelRatioF())
        h = int(self.height() * self.devicePixelRatioF())
        self.controller.render(fbo, w, h)

    def shutdown(self):
        if self.controller is not None:
            self.makeCurrent()
            self.controller.close()
            self.controller = None
            self.doneCurrent()


@pytest.fixture(scope="module")
def preview(qapp):
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)

    window = QMainWindow()
    widget = _PreviewTestWidget()
    window.setCentralWidget(widget)
    window.resize(320, 240)
    window.show()

    pump_events(qapp, lambda: widget.controller is not None)

    yield widget

    widget.shutdown()
    window.close()
    window.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def test_is_mpv_available():
    assert is_mpv_available() is True


def test_controller_connects_via_real_gl_context(preview):
    assert preview.controller is not None


def test_gpu_decoding_interop_is_not_prepared(preview):
    # プレビューは GPU のデコードを使わないので、受け渡しの用意(約 200 MB)をさせない(2026-11-19)
    assert preview.controller._mpv.get_property("gpu-hwdec-interop") == "no"


def _current_frame_or_none(controller):
    # ファイル読込直後はfps未確定でMPVIPCErrorが出うる（design doc §2、
    # get_current_frame()参照）。ポーリング中は「まだ準備できていない」
    # として扱う。
    try:
        return controller.get_current_frame()
    except MPVIPCError:
        return None


def test_load_file_and_playback_advances(preview, sample_video, qapp):
    preview.controller.load_file(sample_video)
    # 開いただけでは再生しない(一時停止で読み込む)。音も初期状態で消えている
    pump_events(qapp, lambda: _current_frame_or_none(preview.controller) is not None, timeout_ms=3000)
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert preview.controller._mpv.pause is True
    assert preview.controller._mpv.mute is True
    assert preview.controller.get_current_frame() == 0

    preview.controller.command(["set", "pause", "no"])
    pump_events(
        qapp,
        lambda: (_current_frame_or_none(preview.controller) or 0) > 0,
        timeout_ms=3000,
    )

    start_frame = _current_frame_or_none(preview.controller) or 0
    pump_events(
        qapp,
        lambda: (_current_frame_or_none(preview.controller) or 0) > start_frame,
        timeout_ms=3000,
    )
    assert preview.controller.get_current_frame() > start_frame


def test_seek_frame(preview, qapp):
    preview.controller.seek_frame(30)
    pump_events(qapp, lambda: 28 <= preview.controller.get_current_frame() <= 32, timeout_ms=3000)

    frame = preview.controller.get_current_frame()
    assert 28 <= frame <= 32


def test_playing_to_the_end_keeps_the_file_open(preview, qapp):
    """最後まで再生(・終わりを越えて送り)しても、ファイルを閉じず最後のフレームで止まる。
    閉じると位置が取れず(Frame: 0)、シークも効かなくなり、固まったように見えた
    (2026-10-08、GR-B の録画で利用者が発見)。"""
    controller = preview.controller
    controller.seek_frame(70)  # 3秒・25fps = 75フレーム
    pump_events(qapp, lambda: (_current_frame_or_none(controller) or 0) >= 68, timeout_ms=3000)
    controller.command(["set", "pause", "no"])
    pump_events(qapp, lambda: controller._mpv.eof_reached is True, timeout_ms=3000)
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert controller._mpv.time_pos is not None
    assert controller.get_current_frame() >= 70

    controller.command(["seek", "5", "relative"])  # 終わりを越える 5 秒送り
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert controller._mpv.time_pos is not None

    controller.command(["set", "pause", "yes"])
    controller.seek_frame(10)
    pump_events(qapp, lambda: 8 <= (_current_frame_or_none(controller) or -1) <= 12, timeout_ms=3000)
    assert 8 <= controller.get_current_frame() <= 12


def test_set_playback_speed(preview):
    preview.controller.set_playback_speed(2.0)
    assert preview.controller._mpv.speed == pytest.approx(2.0)

    preview.controller.set_playback_speed(1.0)
    assert preview.controller._mpv.speed == pytest.approx(1.0)


def test_audio_device_list_and_selection(preview):
    controller = preview.controller
    devices = controller.get_audio_devices()
    assert devices[0][0] == "auto"
    assert all(name and description for name, description in devices)
    assert controller.set_audio_device(devices[-1][0]) is True
    assert controller._mpv.audio_device == devices[-1][0]
    # 一覧に無い出力先(外したUSB機器等)は自動にする
    assert controller.set_audio_device("pipewire/not-connected") is False
    assert controller._mpv.audio_device == "auto"
    # 旧設定の既定値"default"は自動と同じ
    assert controller.set_audio_device("default") is True
    assert controller._mpv.audio_device == "auto"


def test_render_updates_frame_ready_count(preview, qapp):
    before = preview.frame_ready_count
    preview.controller.set_playback_speed(1.0)
    preview.controller.command(["set", "pause", "no"])
    pump_events(qapp, lambda: preview.frame_ready_count > before, timeout_ms=3000)
    assert preview.frame_ready_count > before


def test_grab_shows_rendered_content(preview, qapp):
    pump_events(qapp, lambda: preview.frame_ready_count > 0, timeout_ms=3000)
    pixmap = preview.window().grab()
    assert not pixmap.isNull()
    assert pixmap.size().width() > 0 and pixmap.size().height() > 0


def test_command_before_connect_raises():
    controller = MPVController()
    with pytest.raises(MPVIPCError):
        controller.command(["loadfile", "/nonexistent.mp4"])


def test_get_current_frame_before_connect_raises():
    controller = MPVController()
    with pytest.raises(MPVIPCError):
        controller.get_current_frame()


def test_render_before_connect_raises():
    controller = MPVController()
    with pytest.raises(MPVIPCError):
        controller.render(0, 100, 100)


def test_close_without_connect_is_safe():
    controller = MPVController()
    controller.close()  # 例外が出ないこと


def test_seek_frame_shows_the_cut_engines_frame_on_broadcast_ts(preview, qapp, tmp_path):
    # 放送TSに近い: 音声が映像より0.4秒先に始まり、GOP15のMPEG-2。以前は
    # (1) mpvのtime-posが音声の先頭を0とするため、フレーム番号がカット処理
    # (映像の先頭が0)より音声の先行分だけ大きく、(2) TSのシークが次の
    # キーフレームまで行き過ぎ、表示されるフレームがカット処理のフレームNと
    # 食い違っていた(Windows実機の実録画で確認: 7〜16フレーム)。
    import numpy as np

    from tests._media_check import _gray_frames, make_interlaced_source
    from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper

    source = make_interlaced_source(
        tmp_path / "broadcast.ts",
        ["-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-bf", "2", "-g", "15", "-b:v", "4M"],
        "ac3", seconds=6, audio_lead=0.4,
    )
    ffmpeg = FFmpegWrapper()
    offset = ffmpeg.get_video_start_offset(source)
    assert offset == pytest.approx(0.4, abs=0.05)
    engine_frames = _gray_frames(source)  # 表示順に並んだフレーム = カット処理のフレーム番号

    controller = preview.controller
    controller.set_frame_timing(ffmpeg.get_file_info(source).fps, offset)
    controller.load_file(source)
    pump_events(qapp, lambda: controller._mpv.time_pos is not None, timeout_ms=5000)
    controller._mpv.pause = True
    try:
        for frame in (37, 98, 141):  # どれもGOPの途中(キーフレームではない)
            rendered = preview.frame_ready_count
            controller.seek_frame(frame)
            # time-posはシーク要求の時点で目標値になるため、シークが終わって
            # 新しいフレームが描画されるまで待つ。
            pump_events(
                qapp,
                lambda: not controller._mpv.seeking and preview.frame_ready_count > rendered,
                timeout_ms=5000,
            )
            pump_events(qapp, lambda: False, timeout_ms=300)
            assert controller.get_current_frame() == frame

            shot = tmp_path / f"shown_{frame}.png"
            controller.command(["screenshot-to-file", str(shot), "video"])
            shown = _gray_frames(shot)[0]
            matched = int(np.argmin(((engine_frames - shown) ** 2).mean(axis=(1, 2))))
            assert matched == frame
    finally:
        controller._mpv.pause = False
