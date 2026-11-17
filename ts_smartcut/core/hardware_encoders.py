"""ハードウェアエンコーダー判定結果の保存・読み込み。

判定は初回起動時に1回だけ行い、設定ディレクトリのhardware_encoders.jsonに
保存する(設定画面の「再判定」で更新できる)。GPU・CPUの構成か ffmpeg のビルドが
保存時と変わった時だけ自動で判定し直す。AppSettingsはプロジェクト
ファイルにも埋め込まれるため、PC固有の判定結果はそこに含めない。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from ts_smartcut.core.settings_manager import get_default_settings_path
from ts_smartcut.ffmpeg.gpu import (
    DETECTION_VERSION,
    HardwareDetection,
    detect_hardware_encoders,
    hardware_fingerprint,
)
from ts_smartcut.i18n.language_manager import english
from ts_smartcut.models import AppSettings
from ts_smartcut.system.dependency_checker import DependencyChecker

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: Optional[HardwareDetection] = None


def detection_path() -> Path:
    return get_default_settings_path().parent / "hardware_encoders.json"


def _tool_path(configured: Optional[Path], default: str) -> str:
    return str(configured) if configured else default


def _ffmpeg(settings: AppSettings) -> str:
    return _tool_path(settings.ffmpeg_path, "ffmpeg")


def _ffmpeg_changed(saved: HardwareDetection, settings: AppSettings) -> bool:
    """判定した時と ffmpeg のビルドが違うか。今の ffmpeg の版が読めない時(見つからない・時間切れ)は
    違うとみなさない(読めないたびに判定し直さない)。"""
    current = DependencyChecker.ffmpeg_build_id(_ffmpeg(settings))
    return current is not None and current != saved.ffmpeg_build


def get_hardware_encoders(settings: AppSettings, force: bool = False) -> HardwareDetection:
    """判定結果を返す。保存済みならそれを使い、無ければ(初回起動)判定して保存する。
    force=Trueで判定し直す。複数スレッドから呼ばれても判定は1回だけ。"""
    global _cached
    with _lock:
        if _cached is not None and not force:
            return _cached
        path = detection_path()
        if not force and path.exists():
            try:
                saved = HardwareDetection.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError) as e:
                logger.warning("Detecting hardware encoders again because the saved result could not be read: %s", e)
            else:
                # 試しエンコードは初回だけ。GPU・CPUの構成(OSの情報を読むだけ)が
                # 変わった時と、判定方法を変えた時だけ判定し直す。
                if saved.version != DETECTION_VERSION:
                    logger.info("Detecting hardware encoders again because the detection method changed")
                elif saved.hardware_id != hardware_fingerprint():
                    logger.info("Detecting hardware encoders again because the GPU/CPU configuration changed")
                elif _ffmpeg_changed(saved, settings):
                    logger.info("Detecting hardware encoders again because ffmpeg changed (%s -> %s)",
                                saved.ffmpeg_build, DependencyChecker.ffmpeg_build_id(_ffmpeg(settings)))
                else:
                    _cached = saved
                    return _cached
        logger.info("Detecting hardware encoders")
        _cached = detect_hardware_encoders(
            ffmpeg_path=_ffmpeg(settings),
            ffprobe_path=_tool_path(settings.ffprobe_path, "ffprobe"),
        )
        logger.info("Hardware encoder detection result: %s", _cached.describe(english).replace("\n", " / "))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_cached.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Could not save the hardware encoder detection result: %s", e)
        return _cached


def clear_cache() -> None:
    """メモリ上の判定結果を捨てる(テスト用)。"""
    global _cached
    with _lock:
        _cached = None
