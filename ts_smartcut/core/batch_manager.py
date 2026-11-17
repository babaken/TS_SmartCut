"""バッチ処理制御 (design doc §8)。

単一バックグラウンドスレッドで実行され、並行起動は禁止する。実行中は
アイテムの追加・削除・並び替えをブロックする（データ整合性のため、
design doc §8.1）。progress_callbackはバックグラウンドスレッドから
呼び出されるため、GUI側で消費する場合は呼び出し側で`root.after()`等を
使ってメインスレッドへマーシャリングすること（design doc §29）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from ts_smartcut.core import script_output
from ts_smartcut.core.output_manager import OutputManager, copy_file
from ts_smartcut.errors import FileIOError, ProcessingCancelledError, TSSmartCutError, ValidationError
from ts_smartcut.ffmpeg.smart_cut import PROCESSING_MODES, ProcessingProgressEvent, SmartCutEngine
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper
from ts_smartcut.models import AppSettings, BatchItem, FileCutInfo, ProcessProgress, removed_ranges
from ts_smartcut.system.disk_space_manager import DiskSpaceManager
from ts_smartcut.system.temp_file_manager import TempFileManager, locked_run_dir, remove_stale_temp_files

logger = logging.getLogger(__name__)

STATUS_IDLE = "Idle"
STATUS_PROCESSING = "Processing"
STATUS_DONE = "Done"
STATUS_ERROR = "Error"
STATUS_SKIPPED = "Skipped"

# 一時フォルダ未設定時に出力先の中に作る一時フォルダの名前
TEMP_SUBDIR_NAME = ".ts_smartcut_temp"

# 特徴の計算(自動カット A3/A4)をしている間の ProcessProgress.current_phase
PHASE_FEATURES = "Features"
# スクリプトの出力(設計書 §6.9)で録画を読んでいる間の ProcessProgress.current_phase
PHASE_SCRIPT = "Script"


class BatchManager:
    def __init__(
        self,
        settings: AppSettings,
        ffmpeg: Optional[FFmpegWrapper] = None,
        hardware=None,
        autocut_recorder=None,
        record_autocut: bool = True,
        feature_worker=None,
    ):
        self.settings = settings
        self.ffmpeg = ffmpeg or FFmpegWrapper(settings)
        # ハードウェアエンコーダーの判定結果(HardwareDetection)。Noneなら
        # 処理時に保存済みの結果を読む(無ければその場で判定する)。
        self.hardware = hardware
        self.autocut_recorder = autocut_recorder
        # 「エンコード開始」用(1件だけの BatchManager)は記録しない(2026-10-08 利用者指示)
        self.record_autocut = record_autocut
        # 記録した録画の特徴(A3/A4)を計算する FeatureWorker。None なら計算しない。
        # 1本ずつ「変換 → 記録 → 特徴の計算 → Done」の順に、このスレッドで続けて計算する
        # (利用者の指示 2026-10-08: Done は変換と特徴の計算が両方終わった印。Done に
        # なった録画は移動してよい。バッチの後にまとめて計算していた時は、その前に
        # 録画を移動されて計算できなかった)。変換と同時には動かさない
        self.feature_worker = feature_worker
        # 中止・スキップで特徴の計算を止める
        self._feature_stop = threading.Event()
        self.output_manager = OutputManager(settings)

        self.items: List[BatchItem] = []
        self.current_index: int = -1
        self.is_running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.stop_requested: bool = False
        self.skip_requested: bool = False
        self._lock = threading.Lock()
        # 中止・スキップの印を立てる・消す所を一度に一つにする(印を消すのと中止が重なって中止が消えないように)
        self._control_lock = threading.Lock()

    # ---- アイテム管理 (実行中はブロック) -------------------------------

    def add_item(
        self,
        file_info: FileCutInfo,
        processing_mode: str = "smart_cut",
        output_path: Optional[Path] = None,
        program_info: Optional[Any] = None,
        output_format: str = script_output.OUTPUT_TS,
        edit_events: Optional[List[dict]] = None,
        proposals: Optional[List[dict]] = None,
    ) -> bool:
        """項目を足す。output_path(保存名称で決めた出力先)が他の項目と同じなら足さない。
        同じ録画は、出力先が違えば何度でも足せる(1本の録画に入った複数の話を分けて変換する、
        2026-10-11)。出力先を決めない項目(パターンの名前で変換の時に決める)は、同じ録画を
        二度足せない(前からの動き)。"""
        with self._lock:
            if self.is_running:
                return False
            if output_path is not None:
                if any(item.output_path is not None and OutputManager.same_output(item.output_path, output_path)
                       for item in self.items):
                    return False
            elif any(item.output_path is None and item.file_info.file_path == file_info.file_path
                     for item in self.items):
                return False
            item = BatchItem(
                order=len(self.items),
                file_info=file_info,
                status=STATUS_IDLE,
                output_path=output_path,
                processing_mode=processing_mode,
                output_format=output_format,
                program_info=program_info,
                edit_events=list(edit_events or []),
                proposals=list(proposals or []),
            )
            self.items.append(item)
            return True

    def remove_item(self, index: int) -> bool:
        with self._lock:
            if self.is_running or not (0 <= index < len(self.items)):
                return False
            self.items.pop(index)
            self._reassign_order()
            return True

    def remove_completed(self) -> int:
        """完了した項目だけを一覧から消し、消した数を返す(バッチリストの「クリア」、
        2026-10-11)。エラー・スキップ・未処理は再試行できるよう残す。実行中は何もしない。"""
        with self._lock:
            if self.is_running:
                return 0
            before = len(self.items)
            self.items = [item for item in self.items if item.status != STATUS_DONE]
            self._reassign_order()
            return before - len(self.items)

    def reorder(self, old_index: int, new_index: int) -> bool:
        with self._lock:
            if self.is_running:
                return False
            if not (0 <= old_index < len(self.items) and 0 <= new_index < len(self.items)):
                return False
            item = self.items.pop(old_index)
            self.items.insert(new_index, item)
            self._reassign_order()
            return True

    def set_processing_mode(self, index: int, mode: str) -> bool:
        """追加済みの項目の処理モードを変える (design doc §6.1)。未処理(Idle)か
        エラー(Error)の項目だけ。実行中は受け付けない。"""
        with self._lock:
            if self.is_running or not (0 <= index < len(self.items)) or mode not in PROCESSING_MODES:
                return False
            item = self.items[index]
            if item.status not in (STATUS_IDLE, STATUS_ERROR):
                return False
            item.processing_mode = mode
            return True

    def set_output_format(self, index: int, output_format: str) -> bool:
        """追加済みの項目の出力形式(TS／AVS／VPY、設計書 §6.9)を変える。処理モードと同じく未処理(Idle)か
        エラー(Error)の項目だけ、実行中は受け付けない。決めてある出力先の拡張子も変えた形式に合わせる。"""
        with self._lock:
            if (self.is_running or not (0 <= index < len(self.items))
                    or output_format not in script_output.OUTPUT_FORMATS):
                return False
            item = self.items[index]
            if item.status not in (STATUS_IDLE, STATUS_ERROR):
                return False
            if item.output_path is not None:
                path = item.output_path.with_suffix(self.output_extension(item.file_info.file_path, output_format))
                if any(i != index and other.output_path is not None and OutputManager.same_output(other.output_path, path)
                       for i, other in enumerate(self.items)):
                    return False
                item.output_path = path
            item.output_format = output_format
            return True

    @staticmethod
    def output_extension(source: Path, output_format: str) -> str:
        """出力の拡張子: TS は元の録画と同じ、AVS／VPY は .avs／.vpy。"""
        return script_output.SCRIPT_EXTENSIONS.get(output_format, Path(source).suffix)

    def _reassign_order(self) -> None:
        for i, item in enumerate(self.items):
            item.order = i

    # ---- 実行制御 -----------------------------------------------------

    def start(self, progress_callback: Optional[Callable[[ProcessProgress], None]] = None) -> bool:
        with self._lock:
            if self.is_running:
                return False
            self.is_running = True
            self.stop_requested = False
            # 止まっている時に押したスキップを残さない(次に変換した項目が Skipped になった、2026-11-17)
            self.skip_requested = False
            self.current_index = 0
            self.ffmpeg.cancel_event.clear()
            self._feature_stop.clear()

        self.thread = threading.Thread(
            target=self._process_batch, args=(progress_callback,), daemon=True
        )
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_requested = True

    def cancel(self) -> None:
        """実行中のffmpegを終了させて処理をやめる(中止ボタン)。処理中の項目は
        E504になり、作りかけの出力ファイルは消す。残りの項目も始めない。"""
        with self._control_lock:
            self.stop_requested = True
            self.ffmpeg.cancel_event.set()
            self._feature_stop.set()

    def skip_current(self) -> None:
        """処理中の項目をスキップする(スキップボタン)。変換中ならその項目の ffmpeg を止め、出力は
        置かずに Skipped にして次の項目へ進む(設計書 §8.2)。特徴の計算中なら計算をやめるだけで、
        変換は終わっているので Done(§8.3)。動いていない時は何もしない。

        以前は変換中に押しても ffmpeg を止めず、出力を置いたのに Skipped になり記録もされず、
        立てたままの印で次の項目の特徴の計算がすぐに止まった(2026-11-17)。"""
        with self._control_lock:
            if not self.is_running:
                return
            self.skip_requested = True
            self.ffmpeg.cancel_event.set()
            self._feature_stop.set()

    def _begin_item(self) -> None:
        """項目の始まりで、前の項目へのスキップの印を消す(エラーの項目で押したスキップが残り、
        次の項目が Skipped になった、2026-11-17)。中止の後は消さない。"""
        with self._control_lock:
            self.skip_requested = False
            if not self.stop_requested:
                self.ffmpeg.cancel_event.clear()
                self._feature_stop.clear()

    def output_paths(self, exclude_index: Optional[int] = None) -> List[Path]:
        """項目の出力先(決まっているもの)。exclude_index の項目を除く(名称変更で自分と比べない)。"""
        with self._lock:
            return [item.output_path for i, item in enumerate(self.items)
                    if item.output_path is not None and i != exclude_index]

    def set_audio_track(self, index: int, track: dict) -> bool:
        """追加済みの項目の音声(出力に入れる音声、設計書 §4.12)を変える。選べる音声の一覧にあるものだけ。
        処理モードと同じく未処理(Idle)かエラー(Error)の項目だけ、実行中は受け付けない。"""
        with self._lock:
            if self.is_running or not (0 <= index < len(self.items)):
                return False
            item = self.items[index]
            if item.status not in (STATUS_IDLE, STATUS_ERROR):
                return False
            if not any(t.get("pid") == track.get("pid") and t.get("index") == track.get("index")
                       for t in item.file_info.audio_tracks):
                return False
            item.file_info.audio_track = dict(track)
            return True

    def can_rename(self, index: int) -> bool:
        """保存名称を変えられるか。未処理・エラー・スキップの項目だけ(完了は出力ができている)。
        実行中は受け付けない。"""
        return (not self.is_running and 0 <= index < len(self.items)
                and self.items[index].status in (STATUS_IDLE, STATUS_ERROR, STATUS_SKIPPED))

    def rename_item(self, index: int, output_path: Path) -> bool:
        """項目の出力先を変える(バッチリストの右クリック「名称変更」)。他の項目と同じなら変えない。"""
        with self._lock:
            if not self.can_rename(index):
                return False
            if any(i != index and item.output_path is not None and OutputManager.same_output(item.output_path, output_path)
                   for i, item in enumerate(self.items)):
                return False
            self.items[index].output_path = Path(output_path)
            return True

    def retry_item(self, index: int) -> bool:
        """指定アイテムをIdleへ戻す。実行中は安全のため受け付けない
        （実行完了・停止後に使う想定）。"""
        if self.is_running or not (0 <= index < len(self.items)):
            return False
        item = self.items[index]
        item.status = STATUS_IDLE
        item.error_code = None
        item.error_message = None
        return True

    # ---- バッチ実行ループ ------------------------------------------------

    def _process_batch(self, progress_callback: Optional[Callable[[ProcessProgress], None]]) -> None:
        start_time = time.monotonic()
        try:
            for index, item in enumerate(self.items):
                if self.stop_requested:
                    break
                if item.status != STATUS_IDLE:
                    continue

                self.current_index = index
                self._begin_item()
                item.status = STATUS_PROCESSING

                try:
                    self._process_item(item, index, start_time, progress_callback)
                except Exception as exc:  # noqa: BLE001 - バッチは1アイテムの失敗で全体を止めない
                    if (isinstance(exc, ProcessingCancelledError) and self.skip_requested
                            and not self.stop_requested):
                        # スキップで ffmpeg を止めた。作りかけの出力は run_<id>/ ごと消えている
                        logger.info("Skipped: %s", item.file_info.file_path)
                        item.status = STATUS_SKIPPED
                        continue
                    # エラーの項目は飛ばして次へ進む(利用者の要件 2026-10-05、design doc §8.3)。
                    # 自動再試行はしない: コーデック非対応や壊れた録画は何度やっても同じ
                    # エラーになる。やり直しはバッチリストの「再試行」で行う。
                    self._handle_item_error(item, exc)
                    continue

                # 変換が終わった(出力を置いた)。記録して特徴を計算してから Done にする(処理中のまま)。
                # 変換の終わり際に押したスキップは、特徴の計算をやめるだけ
                job = self._record_autocut_done(item)
                if job is not None:
                    self._compute_features(job, index, start_time, progress_callback)
                item.status = STATUS_DONE
        finally:
            self.is_running = False
            self.current_index = -1

    def _compute_features(self, job, index: int, start_time: float,
                          progress_callback: Optional[Callable[[ProcessProgress], None]]) -> None:
        """記録した録画の特徴を計算して保存する(このスレッドで。失敗しても Done にする)。
        中止・スキップした時は、そこまでの分を保存する(tools/compute_missing_features.py で
        後から計算し直せる)。"""
        worker = self.feature_worker
        # 設定で ffmpeg の場所を変えた場合に合わせる
        worker.ffmpeg_path = self.ffmpeg.ffmpeg_path
        worker.ffprobe_path = self.ffmpeg.ffprobe_path
        total = max(len(self.items), 1)
        frames = max(int(self.items[index].file_info.total_frames), 1)

        def progress(pictures: int) -> None:
            if progress_callback is None:
                return
            progress_callback(ProcessProgress(
                current_file=index + 1,
                total_files=total,
                current_phase=PHASE_FEATURES,
                progress_percent=min(pictures / frames * 100.0, 100.0),
                elapsed_time=time.monotonic() - start_time,
                estimated_remaining=0.0,
            ))

        progress(0)
        try:
            worker.compute_now(job, stop_event=self._feature_stop, progress=progress)
        except Exception as e:  # noqa: BLE001 - 自動カットの失敗でバッチを止めない
            logger.warning("Feature computation failed: %s (%s)", job.file_path, e)
        if self._feature_stop.is_set() and not self.stop_requested:
            self._feature_stop.clear()  # スキップ: 次の録画は計算する

    def _record_autocut_done(self, item: BatchItem):
        """変換が終わった時に自動カットの記録(A2)を行い、特徴を計算する仕事(FeatureJob)を
        返す(計算しない時は None)。"""
        if not self.record_autocut:
            return None
        try:
            from ts_smartcut.autocut.recorder import AutoCutRecorder

            recorder = self.autocut_recorder or AutoCutRecorder()
            session_id = recorder.record_batch_done(
                file_info=item.file_info,
                # スクリプトの出力は処理モードを使わないので、形式(avs／vpy)を残す
                processing_mode=(item.output_format if script_output.is_script_format(item.output_format)
                                 else item.processing_mode),
                program_info=item.program_info,
                edit_events=item.edit_events,
                proposals=item.proposals,
                output_name=item.output_path.name if item.output_path is not None else None,
            )
            if session_id is not None and self.feature_worker is not None:
                return self._feature_job(recorder, session_id, item)
        except Exception as e:
            logger.warning("Recording the cut in the automatic cut history failed: %s", e)
        return None

    @staticmethod
    def _feature_job(recorder, session_id: int, item: BatchItem):
        from ts_smartcut.autocut.feature_worker import FeatureJob
        from ts_smartcut.autocut.features import KeepRange

        session = recorder.db.get_session(session_id)
        info = item.file_info
        # 録画の有効なカットすべての区間(話ごとに分けて変換した録画は、前に変換した話の区間も。
        # この項目の区間だけだと、前の話の本編を残さなかった部分として扱ってしまう)
        keep = [KeepRange(r["start_frame"], r["end_frame"], bool(r["is_excluded"]))
                for r in recorder.db.get_active_cut_ranges(session["recording_id"])]
        return FeatureJob(
            recording_id=session["recording_id"],
            file_path=info.file_path,
            fps=info.fps,
            keep=keep or [KeepRange(c.start_frame, c.end_frame, c.is_excluded) for c in info.cuts],
            service_id=getattr(item.program_info, "service_id", None),
        )

    def _handle_item_error(self, item: BatchItem, exc: Exception) -> None:
        item.status = STATUS_ERROR
        if isinstance(exc, TSSmartCutError):
            item.error_code = exc.code
            item.error_message = exc.message
        else:
            item.error_code = "E999"
            item.error_message = str(exc)
        if isinstance(exc, ProcessingCancelledError):
            logger.info("Processing cancelled: %s", item.file_info.file_path)
            return
        logger.error("[%s] %s: %s", item.error_code, item.file_info.file_path,
                     getattr(exc, "log_message", None) or str(exc))

    def _hardware(self):
        """初回起動時のハードウェアエンコーダー判定結果。テスト等で差し替えられる
        ようにhardware属性があればそれを使う。"""
        if self.hardware is not None:
            return self.hardware
        from ts_smartcut.core.hardware_encoders import get_hardware_encoders

        return get_hardware_encoders(self.settings)

    def _process_item(
        self,
        item: BatchItem,
        index: int,
        start_time: float,
        progress_callback: Optional[Callable[[ProcessProgress], None]],
    ) -> None:
        # バッチに追加したあとで元ファイルの名前を変えた・消した場合。確かめないと
        # ffprobeの失敗(E102「ffprobe実行エラー」)になり、原因が分かりにくかった
        # (Windows実機、2026-10-05)。
        if not item.file_info.file_path.is_file():
            raise FileIOError("E001", key="messages.file_not_found", path=item.file_info.file_path)

        # file_info.cutsは残す区間。エンジンには削除する区間を渡す。
        if not item.file_info.cuts:
            raise ValidationError("E201", key="exceptions.no_keep_ranges")
        if script_output.is_script_format(item.output_format):
            self._write_script(item, index, start_time, progress_callback)
            return
        removed = removed_ranges(item.file_info.total_frames, item.file_info.cuts)

        # 追加の後で同じ名前のファイルができていれば連番の名前にする(2026-10-22)
        reserved = self.output_paths(exclude_index=index)
        output_path = self.output_manager.generate_output_path(
            item.file_info.file_path, item.output_path, reserved=reserved
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 一時ファイル(出力とほぼ同じ大きさになる)の置き場所。未設定なら
        # OSの一時フォルダではなく出力先の中に作る。Linuxの/tmpはメモリ上の
        # 小さな領域(tmpfs)のことが多く、数GBの放送TSで容量不足になった
        # (実録画で確認、2026-10-04)。
        temp_dir = self.settings.temp_directory or output_path.parent / TEMP_SUBDIR_NAME
        temp_dir.mkdir(parents=True, exist_ok=True)

        DiskSpaceManager.check_before_processing(
            item.file_info, removed, output_path.parent, temp_dir
        )

        # 異常終了等で残った前回の一時ファイルは再利用できないので消す (design doc §11.2)
        remove_stale_temp_files(temp_dir)

        def _on_part_progress(event: ProcessingProgressEvent) -> None:
            if progress_callback is None:
                return
            fraction = (event.part_index + event.phase_progress) / max(event.total_parts, 1)
            elapsed = time.monotonic() - start_time
            progress_callback(
                ProcessProgress(
                    current_file=index + 1,
                    total_files=len(self.items),
                    current_phase=event.phase,
                    progress_percent=fraction * 100,
                    elapsed_time=elapsed,
                    estimated_remaining=self._estimate_remaining(elapsed, index, fraction),
                    current_step=event.part_index,
                    total_steps=event.total_parts,
                    source_frame=event.source_frame,
                )
            )

        try:
            with locked_run_dir(temp_dir) as run_dir:
                engine = SmartCutEngine(self.ffmpeg, TempFileManager(run_dir), self.settings, self._hardware())
                # 結合は処理のフォルダの中で行い、できあがってから出力先へ置く(2026-10-22)。
                # 作りかけの出力を出力先に残さず、処理中に同じ名前のファイルができても
                # 上書きしない(失敗・中止の時は処理のフォルダごと消える)。エンジンの
                # TempFileManager は処理の最後に自分の一時ファイルを消すので、そこには登録しない
                joined = run_dir / f"output{output_path.suffix}"
                _effective_mode, warning = engine.process(
                    item.file_info,
                    removed,
                    joined,
                    mode=item.processing_mode,
                    progress_callback=_on_part_progress,
                )
                # 結合の後に押したスキップ・中止でも出力は置かない(置き終わった後なら Done のまま、2026-11-17)
                self._stop_if_cancelled()
                output_path = self.output_manager.place_output(
                    joined, output_path, item.file_info.file_path, reserved=reserved,
                    cancel_event=self.ffmpeg.cancel_event,
                )
        finally:
            if self.settings.temp_directory is None:
                try:
                    temp_dir.rmdir()  # 空なら片付ける(他の処理が使用中なら残る)
                except OSError:
                    pass

        item.output_path = output_path
        if warning:
            logger.warning("%s: %s", item.file_info.file_path, warning)

    def _write_script(self, item: BatchItem, index: int, start_time: float,
                      progress_callback: Optional[Callable[[ProcessProgress], None]]) -> None:
        """出力形式 AVS／VPY: カット情報のスクリプトと、タイムコード・残す区間の音声(FLAC)を書く(設計書 §6.9)。
        動画は変換しない。残す区間を絵の番号に直すため録画全体を 1 回読み、音声を書くためにもう 1 回読む。
        出力先・名前の決め方・上書きしない、は動画の出力と同じ。名前は 3 つそろえて確保してから中身を書き、
        失敗・中止の時は確保したファイルを消す。音声は処理のフォルダで作ってから置く。"""
        fmt = item.output_format
        source = item.file_info.file_path
        extension = script_output.SCRIPT_EXTENSIONS[fmt]
        template_path = self.settings.avs_template if fmt == script_output.OUTPUT_AVS else self.settings.vpy_template
        template = script_output.load_template(fmt, template_path)
        script_output.check_template(template)  # 録画を読む前に知らせる
        reserved = self.output_paths(exclude_index=index)
        output_path = self.output_manager.generate_output_path(source, item.output_path, reserved=reserved,
                                                               extension=extension)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        def progress(percent: float) -> None:
            if progress_callback is not None:
                progress_callback(ProcessProgress(
                    current_file=index + 1, total_files=len(self.items), current_phase=PHASE_SCRIPT,
                    progress_percent=percent, elapsed_time=time.monotonic() - start_time, estimated_remaining=0.0,
                ))

        progress(0.0)
        plan = script_output.plan_script(self.ffmpeg, item.file_info)
        progress(50.0)
        self._stop_if_cancelled()

        temp_dir = self.settings.temp_directory or output_path.parent / TEMP_SUBDIR_NAME
        temp_dir.mkdir(parents=True, exist_ok=True)
        remove_stale_temp_files(temp_dir)
        paths = self.output_manager.claim_outputs(output_path, source, reserved=reserved,
                                                  suffixes=script_output.COMPANION_SUFFIXES)
        script_path, timecode_path, audio_path = paths
        try:
            with locked_run_dir(temp_dir) as run_dir:
                audio = run_dir / f"audio{script_output.AUDIO_SUFFIX}"
                self.ffmpeg.run(script_output.audio_args(plan, source, audio))
                self._stop_if_cancelled()
                try:
                    os.replace(audio, audio_path)  # 確保した空のファイルを置き換える
                except OSError:
                    copy_file(audio, audio_path, self.ffmpeg.cancel_event)  # 一時フォルダが別のドライブ
            self._stop_if_cancelled()
            # L-SMASH Works の索引(.lwi)はスクリプトと同じフォルダに作らせる(録画の横には作らない)
            video_expression, audio_expression = script_output.source_expressions(
                fmt, source, audio_path, cache_dir=script_path.parent)
            script = script_output.render_script(fmt, template, video_expression, audio_expression, plan)
            script_path.write_text(script, encoding="utf-8", newline="\n")
            timecode_path.write_text(script_output.timecode_text(plan.frame_count, plan.fps), encoding="utf-8",
                                     newline="\n")
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        finally:
            if self.settings.temp_directory is None:
                try:
                    temp_dir.rmdir()
                except OSError:
                    pass
        item.output_path = script_path

    def _stop_if_cancelled(self) -> None:
        """スキップ・中止が押されていれば E504(出力を置く前・スクリプトを書く前に確かめる)。ffmpeg が
        動いていない間に押されると、ffmpeg の中止では止まらず、出力を置いて Done にしていた(2026-11-17 の
        レビュー)。"""
        if self.ffmpeg.cancel_event.is_set():
            raise ProcessingCancelledError("E504", key="exceptions.cancelled")

    def _estimate_remaining(self, elapsed: float, index: int, fraction: float) -> float:
        completed_files = index + fraction
        if completed_files <= 0:
            return 0.0
        per_file = elapsed / completed_files
        remaining_files = max(len(self.items) - completed_files, 0)
        return per_file * remaining_files
