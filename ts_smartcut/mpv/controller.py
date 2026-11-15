"""mpv render API統合 (design doc §2、単一のクロスプラットフォーム実装)。

`vo=libmpv` (mpvがAPI経由の描画専用に提供するビデオ出力) と
`mpv_render_context` を使い、mpvに自前のウィンドウを持たせず、呼び出し側
のOpenGLコンテキストへフレームを描画させる。OS固有の「ウィンドウ埋め込み」
実装が不要なため、Windows/Linuxで同一の実装を使う（design doc §2、
CLAUDE.md — OS固有の問題が実際に見つかった場合のみ分割する）。
"""

from __future__ import annotations

import locale
import threading
from typing import Any, Callable, List, Optional, Tuple

from ts_smartcut.errors import MPVIPCError, MPVStartupError
from ts_smartcut.mpv import libmpv
from ts_smartcut.mpv.base import AUTO_AUDIO_DEVICE, MPVControllerBase
from ts_smartcut.time_utils import time_to_frame


class MPVController(MPVControllerBase):
    def __init__(self, **extra_mpv_options: Any):
        self._extra_mpv_options = extra_mpv_options
        self._mpv = None
        self._render_ctx = None
        self._proc_address_fn: Optional[libmpv.GetProcAddressFn] = None  # GC対策で参照を保持する
        self._timing_fps: Optional[float] = None
        self._start_offset = 0.0
        self._audio_pid: Optional[int] = None  # 選んだ音声の PID (設計書 §4.12)。None = mpv の既定
        # track-list の知らせ (mpv のスレッド) と close() を分ける。閉じかけ (terminate の途中) のハンドルを
        # 知らせの側が読んで落ちた (Linux、音声を選んだ録画を開いて閉じると毎回。2026-10-27)
        self._track_lock = threading.Lock()
        self._closing = False

    def connect(self, get_proc_address: Callable[[bytes], int]) -> None:
        try:
            libmpv.load_library()
        except OSError as e:
            raise MPVStartupError("E103", key="exceptions.mpv_not_found", error=e)

        # libmpvはLC_NUMERICが"C"であることを要求する。QApplication構築時にQtが
        # ロケールをリセットする（実機でセグフォルトを確認済み）ため、作る直前に設定する。
        locale.setlocale(locale.LC_NUMERIC, "C")

        # hr-seek-demuxer-offset: MPEG-TSでは目標が途中のフレームだと、デマルチ
        # プレクサのシークが次のキーフレームまで行き過ぎ、正確シークでも目標より
        # 後ろ(実録画で約16フレーム)に止まっていた。2秒手前からデコードさせる
        # (ffmpeg側のcoarse_seek_frameと同じ考え方。CLAUDE.md "Cut accuracy rules")。
        # pause/mute: 起動時は止まっていて音も出ない(ファイルを開いても自動で再生しない。
        # 利用者の要件 2026-10-06)。
        # keep-open: 最後まで再生した(終わりを越えて送った)時にファイルを閉じず、最後の
        # フレームで止まる。既定(no)では閉じて idle になり、位置が取れず(Frame: 0)
        # シークも効かず、固まったように見えた(2026-10-08、GR-B の録画で利用者が発見)。
        options = {
            "idle": "yes", "keep_open": "yes", "vo": "libmpv", "hr_seek_demuxer_offset": "2",
            "pause": "yes", "mute": "yes", **self._extra_mpv_options,
        }
        try:
            self._mpv = libmpv.Mpv(**options)
        except Exception as e:
            raise MPVStartupError("E103", key="exceptions.mpv_start_failed", error=e)
        # 番組が始まってから PMT に加わる音声 (GR-B の 2 つ目) は、ファイルを開いた時の一覧に無いことが
        # ある。一覧が変わったら、選んだ音声をもう一度選ぶ
        self._closing = False
        self._mpv.observe_property("track-list", self._on_track_list)

        def _on_get_proc_address(_ctx, name: bytes) -> int:
            return get_proc_address(name) or 0

        self._proc_address_fn = libmpv.GetProcAddressFn(_on_get_proc_address)

        try:
            self._render_ctx = libmpv.MpvRenderContext(self._mpv, self._proc_address_fn)
        except Exception as e:
            raise MPVStartupError("E103", key="exceptions.mpv_render_init_failed", error=e)

    def render(self, fbo: int, width: int, height: int) -> None:
        self._ensure_render_ctx()
        try:
            self._render_ctx.render(fbo, width, height, flip_y=True, internal_format=0x8058)  # GL_RGBA8
            self._render_ctx.report_swap()
        except Exception as e:
            raise MPVIPCError("E104", key="exceptions.mpv_draw_failed", error=e)

    def set_update_callback(self, callback: Callable[[], None]) -> None:
        self._ensure_render_ctx()
        self._render_ctx.set_update_callback(callback)

    def command(self, cmd: List[Any]) -> Any:
        self._ensure_connected()
        if not cmd:
            raise ValueError("cmd must be a non-empty list")
        try:
            return self._mpv.command(*cmd)
        except Exception as e:
            raise MPVIPCError("E104", key="exceptions.mpv_command_failed", command=cmd, error=e)

    def close(self) -> None:
        # render context解放はGLコンテキストのカレント状態を要求するため、
        # mpv本体より先に解放する（呼び出し側はGLコンテキストが有効な
        # 状態でclose()を呼ぶこと。design doc §2、base.py参照）。
        if self._render_ctx is not None:
            self._render_ctx.free()
            self._render_ctx = None
        if self._mpv is not None:
            with self._track_lock:
                # 知らせの途中なら終わるのを待ち、以後の知らせは何もしない
                self._closing = True
                try:
                    self._mpv.unobserve_property("track-list", self._on_track_list)
                except Exception:  # noqa: BLE001 - 監視を外せなくても、知らせの側は _closing を見て戻る
                    pass
            self._mpv.terminate()
            self._mpv = None
        self._proc_address_fn = None

    def set_frame_timing(self, fps: Optional[float], start_offset: float) -> None:
        self._timing_fps = fps
        self._start_offset = start_offset

    # フレーム番号はカット処理と同じく「映像の先頭が0」。mpvのtime-posは
    # ファイル全体の先頭(放送TSでは映像より先に始まる音声)が0なので、その差
    # (_start_offset)を足し引きする。フレームNは表示時刻が(N-0.5)/fps以上の
    # 最初のフレーム(smart_cut._boundaryと同じ定義。ソフトテレシネのフレームは
    # N/fpsの格子から最大半フレームずれる)。正確シークは目標以上の最初の
    # フレームに止まるため、目標を(N-0.5)/fpsにする。

    def seek_frame(self, frame: int) -> None:
        self._ensure_connected()
        fps = self._timing_fps or self._get_fps()
        target_time = max(self._start_offset + (frame - 0.5) / fps, 0.0)
        try:
            self._mpv.seek(target_time, reference="absolute", precision="exact")
        except Exception as e:
            raise MPVIPCError("E104", key="exceptions.mpv_seek_failed", frame=frame, error=e)

    def get_current_frame(self) -> int:
        self._ensure_connected()
        fps = self._timing_fps or self._get_fps()
        time_pos = self._mpv.time_pos
        if time_pos is None:
            # 読込直後で位置が未確定。estimated-frame-numberもtime-pos由来の
            # ためオフセットを考慮しないが、位置が確定するまでの一時的な値。
            frame = self._mpv.estimated_frame_number
            return int(frame) if frame is not None else 0
        return max(time_to_frame(time_pos - self._start_offset, fps), 0)

    def set_playback_speed(self, speed: float) -> None:
        self._ensure_connected()
        self._mpv.speed = speed

    def set_audio_pid(self, pid: Optional[int]) -> bool:
        self._ensure_connected()
        self._audio_pid = pid
        return self._apply_audio_pid()

    def _apply_audio_pid(self) -> bool:
        """選んだ PID の音声を鳴らす (mpv の track-list の src-id は TS の PID)。まだ一覧に無ければ False。"""
        mpv = self._mpv
        if mpv is None:
            return False
        if self._audio_pid is None:
            mpv.aid = "auto"
            return True
        for track in mpv.track_list or []:
            if track.get("type") == "audio" and track.get("src-id") == self._audio_pid:
                if mpv.aid != track["id"]:
                    mpv.aid = track["id"]
                return True
        return False

    def _on_track_list(self, _name, _value) -> None:
        # mpv のスレッドから呼ばれる。失敗してもプレビューの音声が変わらないだけ。閉じかけなら触らない
        with self._track_lock:
            if self._closing or self._audio_pid is None:
                return
            try:
                self._apply_audio_pid()
            except Exception:  # noqa: BLE001
                pass

    def set_volume(self, volume: int) -> None:
        self._ensure_connected()
        self._mpv.volume = volume

    def set_mute(self, muted: bool) -> None:
        self._ensure_connected()
        self._mpv.mute = muted

    def get_audio_devices(self) -> List[Tuple[str, str]]:
        self._ensure_connected()
        return [(d["name"], d.get("description") or d["name"]) for d in self._mpv.audio_device_list or []]

    def set_audio_device(self, name: str) -> bool:
        self._ensure_connected()
        if name in ("", "default", AUTO_AUDIO_DEVICE):
            # 既定の出力先なら一覧は要らない。一覧を取るとmpvは(ao=nullでも)音声
            # ドライバーを初期化する — WindowsではWASAPIを初期化した複数のmpvを
            # 終了したあとに配色を変えると落ちる(tests/conftest.py参照)。
            self._mpv.audio_device = AUTO_AUDIO_DEVICE
            return True
        found = name in [n for n, _ in self.get_audio_devices()]
        self._mpv.audio_device = name if found else AUTO_AUDIO_DEVICE
        return found

    def _get_fps(self) -> float:
        fps = self._mpv.container_fps or self._mpv.estimated_vf_fps
        if not fps:
            raise MPVIPCError("E104", key="exceptions.mpv_no_fps")
        return float(fps)

    def _ensure_connected(self) -> None:
        if self._mpv is None:
            raise MPVIPCError("E104", key="exceptions.mpv_not_connected")

    def _ensure_render_ctx(self) -> None:
        if self._mpv is None or self._render_ctx is None:
            raise MPVIPCError("E104", key="exceptions.mpv_no_render_context")
