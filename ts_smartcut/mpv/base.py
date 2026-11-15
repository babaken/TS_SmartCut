"""mpv制御の抽象インターフェース (design doc §2、render API改訂版)。

mpvはウィンドウ埋め込み(`wid`)ではなくrender API
(`mpv_render_context`)で使う。mpvは自分のウィンドウを持たず、呼び出し側
（Qt6の`QOpenGLWidget`、design doc §2.2）が用意したOpenGLコンテキストへ
描画要求されたときだけフレームを描く。この抽象自体はQtに依存しない
（get_proc_address/updateコールバックは共にプレーンなPython callable）。
Qtとのスレッド安全なブリッジ（シグナル/スロット経由の再描画）は呼び出し
側（gui/preview_widget.py）の責務。

mpv制御はOS依存実装から分離し続けるが、render APIはOSごとの「ウィンドウ
埋め込み」実装を必要としないため、通常は単一のクロスプラットフォーム
実装（mpv/controller.py）で足りる（design doc §2、CLAUDE.md）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# mpvの音声出力先の自動選択(OSの既定の出力先)。旧設定の"default"も同じ扱い。
AUTO_AUDIO_DEVICE = "auto"


class MPVControllerBase(ABC):
    @abstractmethod
    def connect(self, get_proc_address: Callable[[bytes], int]) -> None:
        """mpvインスタンスとrender contextを初期化する。
        get_proc_addressはOpenGL関数ポインタ解決用のコールバック
        （例: QOpenGLContext.getProcAddress）。呼び出し側のOpenGL
        コンテキストが有効な状態で呼ぶこと（例: QOpenGLWidget.initializeGL()
        の中）。失敗時はMPVStartupError(E103)を送出する。"""

    @abstractmethod
    def render(self, fbo: int, width: int, height: int) -> None:
        """現在のフレームを指定したFBOへ描画する。呼び出し側の
        paintGL()相当のタイミングで、対応するOpenGLコンテキストが
        カレントな状態で呼ぶこと。失敗時はMPVIPCError(E104)を送出する。"""

    @abstractmethod
    def set_update_callback(self, callback: Callable[[], None]) -> None:
        """新フレームが描画可能になった際にmpvから呼ばれるコールバックを
        登録する。**このコールバックはmpv内部のスレッドから呼ばれる。**
        callback自身の中でGUIウィジェットを直接操作しないこと。呼び出し
        側でQtのシグナル/スロット（キュー接続）等、スレッドセーフな経路
        でメインスレッドへ再描画要求を渡すこと。"""

    @abstractmethod
    def command(self, cmd: List[Any]) -> Any:
        """生のmpvコマンドを実行する（例: ["loadfile", path]）。
        通信に失敗した場合はMPVIPCError(E104)を送出する。"""

    @abstractmethod
    def close(self) -> None:
        """render contextとmpvインスタンスを解放する。呼び出し側の
        OpenGLコンテキストがまだ有効な状態で呼ぶこと
        （render context解放はGLコンテキストのカレント状態を要求する）。"""

    @abstractmethod
    def seek_frame(self, frame: int) -> None:
        """指定フレーム番号へフレーム精度でシークする。"""

    @abstractmethod
    def get_current_frame(self) -> int:
        """現在の再生位置をフレーム番号で取得する。"""

    @abstractmethod
    def set_frame_timing(self, fps: Optional[float], start_offset: float) -> None:
        """フレーム番号⇔再生位置の換算に使うfpsと、映像の先頭がファイル全体の
        先頭から遅れる秒数を設定する(カット処理と同じフレーム番号にするため)。
        fps=Noneならmpvが報告するfpsを使う。"""

    @abstractmethod
    def set_playback_speed(self, speed: float) -> None:
        """再生速度を設定する (design doc §2.4: 0.25x-2.0x)。"""

    @abstractmethod
    def set_volume(self, volume: int) -> None:
        """音量を設定する (design doc §4.5: Ctrl+ホイールで調整、0-100)。"""

    @abstractmethod
    def get_audio_devices(self) -> List[Tuple[str, str]]:
        """音声の出力先の一覧を(名前, 表示名)で返す。先頭は自動選択("auto")。"""

    @abstractmethod
    def set_audio_device(self, name: str) -> bool:
        """音声の出力先を設定する。一覧に無い名前(外したUSB機器等)の時は
        自動選択にしてFalseを返す。"""

    @abstractmethod
    def set_mute(self, muted: bool) -> None:
        """音を消す/戻す(音量の設定は変えない)。"""

    def set_audio_pid(self, pid: Optional[int]) -> bool:
        """鳴らす音声を TS の PID で選ぶ(設計書 §4.12)。None なら mpv の既定。まだ見つからない音声は、
        見つかった時に選ぶ(戻り値は今選べたか)。"""
        return False

    def load_file(self, path: Path) -> None:
        """メディアファイルを読み込む。command()の上に実装される
        共通の便宜メソッド。開いただけでは再生しない(一時停止で読み込む)。
        mpvのpauseはファイルをまたいで残るので、読み込むたびに止める。"""
        self.command(["set", "pause", "yes"])
        self.command(["loadfile", str(path)])
