"""特徴の計算と保存 (自動カット計画 A3/A4)。

バッチ処理は、1本ずつ変換して記録した後に compute_now() でその録画の特徴を計算し、
それから Done にする(利用者の指示 2026-10-08。Done になった録画は移動してよい)。
1本あたり 45〜50分の 1080i 番組で 30〜90 秒(HDD の録画は長め)。
submit() は裏のスレッドで順に計算する(tools/compute_missing_features.py 等)。

計算中はアプリを閉じられない(利用者の指示 2026-10-08。DB には録画の場所を
保存しないので、閉じると続きを計算できない)。録画が消された時は、計算できた
分だけ保存する(complete=False)。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, List, Optional, Union

from ts_smartcut.autocut.features import KeepRange, RecordingFeatures, compute_features
from ts_smartcut.autocut.logo import update_station_logo

logger = logging.getLogger(__name__)


@dataclass
class FeatureJob:
    recording_id: int
    file_path: Path
    fps: float
    keep: List[KeepRange] = field(default_factory=list)
    service_id: Optional[int] = None  # 録画した番組のサービス(音声・字幕の構成を見る PMT)


class _AnyEvent:
    """どれかが立っていれば立っているとみなす(compute_features は is_set() だけを使う)。"""

    def __init__(self, *events: Optional[threading.Event]):
        self._events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


class FeatureWorker:
    def __init__(
        self,
        db=None,
        ffmpeg_path: Union[str, Path] = "ffmpeg",
        ffprobe_path: Union[str, Path] = "ffprobe",
        compute: Callable[..., RecordingFeatures] = compute_features,
    ):
        self._db = db
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self._compute = compute
        self._queue: Deque[FeatureJob] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False  # _run が動いているか(_lock の中で読み書きする)
        self._current: Optional[FeatureJob] = None  # 計算中の録画(_lock の中で読み書きする)
        self._now_active = 0  # compute_now で計算中の数(_lock の中で読み書きする)
        self._idle = threading.Event()
        self._idle.set()
        # 最後まで計算して保存した録画(しきい値の自動補正の対象の局を決める。take_saved_recordings)
        self._saved: List[int] = []

    @property
    def db(self):
        if self._db is None:
            from ts_smartcut.autocut.history_db import CutHistoryDB

            self._db = CutHistoryDB()
        return self._db

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return not self._idle.is_set() or self._now_active > 0

    @property
    def pending_count(self) -> int:
        """計算中と、まだ始めていない録画の数。"""
        with self._lock:
            return len(self._queue) + (1 if self._current is not None else 0) + self._now_active

    def compute_now(self, job: FeatureJob, stop_event: Optional[threading.Event] = None,
                    progress: Optional[Callable[[int], None]] = None) -> None:
        """この呼び出しのスレッドで1本計算して保存する(バッチ処理)。stop_event か stop() で
        止めると、そこまでの分を保存する。progress は読み込んだコマの数で呼ばれる。"""
        with self._lock:
            self._now_active += 1
        try:
            self._process(job, _AnyEvent(self._stop, stop_event), progress)
        finally:
            with self._lock:
                self._now_active -= 1

    def submit(self, jobs: List[FeatureJob]) -> None:
        """計算する録画を加え、裏のスレッドが止まっていれば始める。"""
        if not jobs:
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._queue.extend(jobs)
            self._idle.clear()
            if not self._running:
                self._running = True
                self._thread = threading.Thread(target=self._run, name="autocut-features", daemon=True)
                self._thread.start()

    def take_saved_recordings(self) -> List[int]:
        """前に呼んだ時から、最後まで計算して保存した録画の id(呼ぶと空になる)。"""
        with self._lock:
            saved, self._saved = self._saved, []
        return saved

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        """計算をやめる(アプリの終了時)。計算中の録画は、そこまでの分を保存する。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                self._current = None
                if self._stop.is_set() or not self._queue:
                    self._queue.clear()
                    self._running = False
                    self._idle.set()
                    return
                job = self._current = self._queue.popleft()
            self._process(job, self._stop)

    def _process(self, job: FeatureJob, stop_event, progress: Optional[Callable[[int], None]] = None) -> None:
        if not job.file_path.is_file():
            logger.info("Not computing features because the recording is missing: %s", job.file_path)
            return
        try:
            features = self._compute(
                job.file_path, job.fps, job.keep,
                ffmpeg_path=self.ffmpeg_path, ffprobe_path=self.ffprobe_path, stop_event=stop_event,
                progress=progress, service_id=job.service_id,
            )
        except Exception:  # noqa: BLE001 - 自動カットの失敗で他の操作を止めない
            logger.warning("Could not compute features: %s", job.file_path, exc_info=True)
            return
        if features.frame_count == 0 and len(features.audio_level) == 0:
            logger.info("Not saving features because nothing could be computed: %s", job.file_path)
            return
        try:
            self.db.save_features(
                job.recording_id, features.to_blob(), features.version, features.complete, features.frame_count
            )
            logger.info(
                "Saved features: recording_id=%d, frames=%d, complete=%s, %s",
                job.recording_id, features.frame_count, features.complete, features.timing,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not save features: %s", job.file_path, exc_info=True)
            return
        if features.complete:
            with self._lock:
                self._saved.append(job.recording_id)
        # 局のロゴ画像(.lgd)の材料。集められなかった時は前の材料があれば残す
        if features.logo_samples is not None:
            try:
                samples = features.logo_samples
                self.db.save_logo_samples(job.recording_id, samples.to_blob(), samples.version, samples.pictures)
                logger.info("Saved logo image samples: recording_id=%d, corners=%s, flat frames=%s",
                            job.recording_id, [c.corner for c in samples.corners], [c.n for c in samples.corners])
            except Exception:  # noqa: BLE001
                logger.warning("Could not save logo image samples: recording_id=%d", job.recording_id, exc_info=True)
        # 局のロゴの型を、この録画を加えて作り直す(A4)
        try:
            station = update_station_logo(self.db, job.recording_id)
            if station is not None:
                logger.info(
                    "Updated the station logo template: recording_id=%d, corners with a logo=%s",
                    job.recording_id, station.has_logo,
                )
        except Exception:  # noqa: BLE001
            logger.warning("Could not update the station logo template: recording_id=%d", job.recording_id, exc_info=True)
