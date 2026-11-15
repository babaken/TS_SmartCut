"""mpv render APIの描画先となるQOpenGLWidget (design doc §2.2)。

mpvはソフト依存 (design doc §12.2, CLAUDE.md) — mpv/libmpvが使えない
環境でもこのウィジェット自体は生成でき、単に映像を描画しないだけに
とどめる。呼び出し側(MainWindow)は`is_connected`を見てプレビュー制限
モードのUI（プレースホルダー表示・操作系の無効化）を出し分けること。

mpvのupdate_cbはmpv内部スレッドから呼ばれるため、Qtのシグナル
（キュー接続）を介してGUIスレッドの再描画へブリッジする
（design doc §2.2、CLAUDE.md: UI更新はQtのシグナル/スロット経由）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ts_smartcut.errors import TSSmartCutError
from ts_smartcut.mpv import create_controller
from ts_smartcut.mpv.base import MPVControllerBase


class _UpdateBridge(QObject):
    """mpvのupdate_cb(mpv内部スレッド)からGUIスレッドへ再描画要求を
    安全に渡すためだけの小さなQObject。"""

    frame_ready = Signal()


class PreviewWidget(QOpenGLWidget):
    # initializeGL()完了後に発火する。QOpenGLWidgetはGLコンテキストが
    # 実際に有効になるまで(=最初のpaint/expose処理まで)initializeGL()を
    # 呼ばないため、コンストラクタ直後にis_connectedを見ても常にFalseに
    # なってしまう。呼び出し側(MainWindow)はこのシグナルで可用性を判定する
    # こと（真偽値はis_connectedの結果）。
    connection_ready = Signal(bool)

    # create_controller()へ渡す追加のmpvオプション。アプリ本体は空のまま
    # (音声あり)。テストは{"ao": "null"}にする(tests/conftest.py参照)。
    mpv_options: Dict[str, Any] = {}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.controller: Optional[MPVControllerBase] = None
        self._update_bridge = _UpdateBridge()
        self._update_bridge.frame_ready.connect(self.update)

    @property
    def is_connected(self) -> bool:
        return self.controller is not None

    def initializeGL(self) -> None:
        def get_proc_address(name: bytes) -> int:
            return self.context().getProcAddress(name) or 0

        self.controller = create_controller(get_proc_address, **self.mpv_options)
        if self.controller is not None:
            self.controller.set_update_callback(self._on_mpv_update)
        self.connection_ready.emit(self.controller is not None)

    def paintGL(self) -> None:
        if self.controller is None:
            return
        fbo = self.defaultFramebufferObject()
        width = int(self.width() * self.devicePixelRatioF())
        height = int(self.height() * self.devicePixelRatioF())
        if width <= 0 or height <= 0:
            return
        self.controller.render(fbo, width, height)

    def _on_mpv_update(self) -> None:
        self._update_bridge.frame_ready.emit()

    # ---- 再生制御 (未接続時は何もしない。有効/無効の出し分けはMainWindow側) --

    def load_file(self, path: Path, fps: Optional[float] = None, start_offset: float = 0.0) -> None:
        """fps/start_offsetはフレーム番号の換算用(MPVControllerBase.set_frame_timing)。"""
        if self.controller is not None:
            self.controller.set_frame_timing(fps, start_offset)
            self.controller.load_file(path)

    def seek_frame(self, frame: int) -> None:
        if self.controller is not None:
            self.controller.seek_frame(frame)

    def get_current_frame(self) -> Optional[int]:
        if self.controller is None:
            return None
        try:
            return self.controller.get_current_frame()
        except TSSmartCutError:
            # 動画読込直後などfps未確定の一時的な状態。呼び出し側は
            # Noneを「まだ取得できない」として扱う。
            return None

    def set_playback_speed(self, speed: float) -> None:
        if self.controller is not None:
            self.controller.set_playback_speed(speed)

    def set_volume(self, volume: int) -> None:
        if self.controller is not None:
            self.controller.set_volume(volume)

    def set_mute(self, muted: bool) -> None:
        if self.controller is not None:
            self.controller.set_mute(muted)

    def set_audio_pid(self, pid: Optional[int]) -> bool:
        if self.controller is None:
            return False
        return self.controller.set_audio_pid(pid)

    def get_audio_devices(self) -> List[Tuple[str, str]]:
        """音声の出力先の一覧。mpvが使えない時は空。"""
        if self.controller is None:
            return []
        return self.controller.get_audio_devices()

    def set_audio_device(self, name: str) -> bool:
        if self.controller is None:
            return False
        return self.controller.set_audio_device(name)

    def command(self, cmd: List[Any]) -> Any:
        if self.controller is not None:
            return self.controller.command(cmd)
        return None

    def cleanup(self) -> None:
        """アプリ終了時に呼ぶ。render context解放にはGLコンテキストが
        カレントな状態が必要なため、ウィジェット破棄前に明示的に呼ぶこと
        （design doc §2、mpv/base.py参照）。"""
        if self.controller is not None:
            self.makeCurrent()
            self.controller.close()
            self.controller = None
            self.doneCurrent()
