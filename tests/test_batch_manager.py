import os
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

import pytest

from ts_smartcut.core.batch_manager import (
    PHASE_FEATURES,
    TEMP_SUBDIR_NAME,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_PROCESSING,
    STATUS_SKIPPED,
    BatchManager,
)
from ts_smartcut.errors import MuxFailedError, ProcessingCancelledError
from ts_smartcut.autocut.history_db import CutHistoryDB
from ts_smartcut.autocut.recorder import AutoCutRecorder
from ts_smartcut.ffmpeg.gpu import HardwareDetection
from ts_smartcut.ffmpeg.wrapper import SourceTiming
from ts_smartcut.models import AppSettings, CutRange, FileCutInfo, ProcessProgress


class FakeFFmpegWrapper:
    """実ffmpegを呼ばずSmartCutEngineを駆動するためのテストダブル。
    run()はfail_first_n_calls回だけ失敗し、その後は成功する。"""

    def __init__(self, is_vfr: bool = False, fail_first_n_calls: int = 0):
        self.ffmpeg_path = "ffmpeg"
        self.ffprobe_path = "ffprobe"
        self._is_vfr = is_vfr
        self.fail_first_n_calls = fail_first_n_calls
        self.run_call_count = 0
        self.cancel_event = threading.Event()

    def is_vfr(self, file_path):
        return self._is_vfr

    def get_keyframes(self, file_path, fps=None, use_cache=True):
        # segment全体がちょうど1つのBodyコピーになるよう先頭と末尾のみ。
        return [0, 100]

    def get_video_packet_times(self, file_path, start, end, fps=None):
        # 25fps・100フレームの等間隔(_file_infoと同じ)
        return [(k / 25.0, 1 / 25.0) for k in range(100) if start <= k / 25.0 <= end]

    def get_first_video_frame_pts(self, file_path):
        return 0.0  # 先頭からデコードできる

    def get_source_timing(self, file_path):
        return SourceTiming(
            video_start=0.0, format_start=0.0, field_order="progressive",
            has_audio=False, audio_packet_duration=0.0,
        )

    def run(self, args: List[str], progress_callback=None, bytes_per_frame=None):
        if self.cancel_event.is_set():
            raise ProcessingCancelledError("E504", "中止しました")
        self.run_call_count += 1
        if self.run_call_count <= self.fail_first_n_calls:
            raise MuxFailedError("E101", "fake ffmpeg failure")
        Path(args[-1]).write_bytes(b"fake output")
        if progress_callback:
            progress_callback(100)  # 出力済みフレーム数


# 処理の前に元ファイルがあるか確かめる(無ければE001)ので、偽のffmpegで処理する
# 録画も空のファイルとして実際に作っておく
_VIDEO_DIR = Path(tempfile.mkdtemp(prefix="ts_smartcut_videos_"))


def _file_info(name: str) -> FileCutInfo:
    path = _VIDEO_DIR / f"{name}.mp4"
    # 中身を名前ごとに変える(記録 DB は中身が同じファイルを同じ動画とみなす)
    path.write_bytes(name.encode("utf-8"))
    return FileCutInfo(
        file_path=path,
        total_frames=100,
        fps=25.0,
        resolution=(320, 240),
        video_codec="h264",
        audio_codec="aac",
        bitrate=100_000,
        duration=4.0,
        file_size=1000,
        cuts=[CutRange(0, 100)],  # 残す区間: 全体
    )


@pytest.fixture
def settings(tmp_path) -> AppSettings:
    return AppSettings(default_output_dir=tmp_path / "out")


@pytest.fixture(autouse=True)
def no_hardware_encoders(monkeypatch):
    """ffmpegは偽物なので、GPUの判定(全GPUで試しエンコードする。GPUのあるPCでは
    数秒かかり、join(timeout=5)を超えた)はせず、ソフトウェアエンコーダーのみとする。"""
    from ts_smartcut.core import hardware_encoders
    from ts_smartcut.ffmpeg.gpu import HardwareDetection

    monkeypatch.setattr(hardware_encoders, "get_hardware_encoders", lambda settings, force=False: HardwareDetection())


@pytest.fixture(autouse=True)
def isolated_autocut_history(monkeypatch, tmp_path):
    """Done時の履歴記録はテストごとの一時DBへ隔離する。"""
    recorder = AutoCutRecorder(CutHistoryDB(tmp_path / "cut_history.db"))
    monkeypatch.setattr("ts_smartcut.autocut.recorder.AutoCutRecorder", lambda: recorder)
    return recorder


# ---- アイテム管理 -----------------------------------------------------

def test_add_item_assigns_order(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    assert mgr.add_item(_file_info("a")) is True
    assert mgr.add_item(_file_info("b")) is True
    assert [i.order for i in mgr.items] == [0, 1]


def test_add_item_rejects_duplicate_path(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    assert mgr.add_item(_file_info("a")) is False
    assert len(mgr.items) == 1


def test_add_item_blocked_while_running(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.is_running = True
    assert mgr.add_item(_file_info("a")) is False


def test_remove_item_reassigns_order(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))
    mgr.add_item(_file_info("c"))

    assert mgr.remove_item(1) is True
    assert [i.file_info.file_path.name for i in mgr.items] == ["a.mp4", "c.mp4"]
    assert [i.order for i in mgr.items] == [0, 1]


def test_remove_item_blocked_while_running(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.is_running = True
    assert mgr.remove_item(0) is False


def test_reorder_items(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))

    assert mgr.reorder(0, 1) is True
    assert [i.file_info.file_path.name for i in mgr.items] == ["b.mp4", "a.mp4"]
    assert [i.order for i in mgr.items] == [0, 1]


def test_reorder_blocked_while_running(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))
    mgr.is_running = True
    assert mgr.reorder(0, 1) is False


# ---- 実行: 正常系 -----------------------------------------------------

def test_start_processes_all_items_successfully(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))

    events: List[ProcessProgress] = []
    assert mgr.start(progress_callback=events.append) is True
    mgr.thread.join(timeout=5)

    assert mgr.is_running is False
    assert [i.status for i in mgr.items] == [STATUS_DONE, STATUS_DONE]
    assert all(i.output_path is not None for i in mgr.items)
    assert len(events) > 0
    assert all(isinstance(e, ProcessProgress) for e in events)


def test_done_batch_item_is_recorded_in_injected_database(settings, isolated_autocut_history):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history)
    mgr.add_item(_file_info("record_done"))

    mgr.start()
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_DONE
    with isolated_autocut_history.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 1


def test_error_batch_item_is_not_recorded(settings, isolated_autocut_history):
    mgr = BatchManager(
        settings, ffmpeg=FakeFFmpegWrapper(fail_first_n_calls=1),
        autocut_recorder=isolated_autocut_history,
    )
    mgr.add_item(_file_info("record_error"))

    mgr.start()
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_ERROR
    with isolated_autocut_history.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0


def test_skipped_batch_item_is_not_recorded(settings, isolated_autocut_history):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history)
    mgr.add_item(_file_info("record_skipped"))

    mgr.start(progress_callback=lambda _event: mgr.skip_current())
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_SKIPPED
    with isolated_autocut_history.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 0


def _recorded_count(recorder) -> int:
    with recorder.db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]


def test_stop_records_only_the_item_that_finished(settings, isolated_autocut_history):
    # 停止は処理中の項目を終えてから止まる。終えた1件だけ記録し、始めなかった項目は記録しない
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history)
    mgr.add_item(_file_info("record_stop_a"))
    mgr.add_item(_file_info("record_stop_b"))

    mgr.start(progress_callback=lambda _event: mgr.stop())
    mgr.thread.join(timeout=5)

    assert [item.status for item in mgr.items] == [STATUS_DONE, STATUS_IDLE]
    assert _recorded_count(isolated_autocut_history) == 1


def test_cancelled_item_is_not_recorded(settings, isolated_autocut_history):
    started = threading.Event()

    class SlowFFmpeg(FakeFFmpegWrapper):
        def run(self, args, progress_callback=None, bytes_per_frame=None):
            if self.run_call_count == 1:
                started.set()
                self.cancel_event.wait(10)
            return super().run(args, progress_callback)

    mgr = BatchManager(
        settings, ffmpeg=SlowFFmpeg(), hardware=HardwareDetection(),
        autocut_recorder=isolated_autocut_history,
    )
    mgr.add_item(_file_info("record_cancel"))
    assert mgr.start() is True
    assert started.wait(5)
    mgr.cancel()
    mgr.thread.join(timeout=5)

    assert (mgr.items[0].status, mgr.items[0].error_code) == (STATUS_ERROR, "E504")
    assert _recorded_count(isolated_autocut_history) == 0


def test_start_encode_manager_does_not_record(settings, isolated_autocut_history):
    # 「エンコード開始」用の BatchManager(record_autocut=False)は Done でも記録しない
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), record_autocut=False)
    mgr.add_item(_file_info("record_start_encode"))

    mgr.start()
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_DONE
    assert _recorded_count(isolated_autocut_history) == 0


class _RecordingFeatureWorker:
    """compute_now() を受け取るだけの FeatureWorker の代わり。呼ばれた時点の各項目の
    状態も記録する(Done は変換と特徴の計算が両方終わった印、2026-10-08)。"""

    def __init__(self, mgr_ref, on_compute=None):
        self.mgr_ref = mgr_ref
        self.computed = []
        self.statuses_at_compute = []
        self.ffmpeg_path = self.ffprobe_path = None
        self.on_compute = on_compute

    def compute_now(self, job, stop_event=None, progress=None):
        self.computed.append(job)
        self.statuses_at_compute.append([item.status for item in self.mgr_ref[0].items])
        if progress is not None:
            progress(50)
        if self.on_compute is not None:
            self.on_compute(stop_event)


def test_features_are_computed_for_each_item_before_it_is_done(settings, isolated_autocut_history):
    ref = []
    worker = _RecordingFeatureWorker(ref)
    progress = []
    mgr = BatchManager(
        settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history, feature_worker=worker,
    )
    ref.append(mgr)
    a, b = _file_info("feature_a"), _file_info("feature_b")
    a.cuts = [CutRange(0, 100, is_excluded=True)]  # 偽の ffmpeg は全体を残す区間しか処理できない
    mgr.add_item(a)
    mgr.add_item(b)

    mgr.start(progress_callback=progress.append)
    mgr.thread.join(timeout=5)

    # 1本ずつ、変換の後・Done の前に計算する(その録画は処理中のまま、次はまだ始まっていない)
    assert worker.statuses_at_compute == [[STATUS_PROCESSING, STATUS_IDLE], [STATUS_DONE, STATUS_PROCESSING]]
    assert [item.status for item in mgr.items] == [STATUS_DONE, STATUS_DONE]
    jobs = worker.computed
    assert [job.file_path for job in jobs] == [a.file_path, b.file_path]
    assert [(r.start, r.end, r.excluded) for r in jobs[0].keep] == [(0, 100, True)]
    assert [(r.start, r.end, r.excluded) for r in jobs[1].keep] == [(0, 100, False)]
    assert jobs[0].fps == 25.0
    with isolated_autocut_history.db.get_connection() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM recordings ORDER BY id")]
    assert [job.recording_id for job in jobs] == ids
    assert worker.ffmpeg_path == "ffmpeg"
    # 進み具合: 特徴の計算(読んだコマ数 / フレーム数)
    features = [p for p in progress if p.current_phase == PHASE_FEATURES]
    assert [(p.current_file, p.total_files) for p in features if p.progress_percent == 50.0] == [(1, 2), (2, 2)]


def test_features_are_not_computed_for_unrecorded_items(settings, isolated_autocut_history):
    ref = []
    worker = _RecordingFeatureWorker(ref)
    mgr = BatchManager(
        settings, ffmpeg=FakeFFmpegWrapper(fail_first_n_calls=1),
        autocut_recorder=isolated_autocut_history, feature_worker=worker,
    )
    ref.append(mgr)
    mgr.add_item(_file_info("feature_error"))
    mgr.start()
    mgr.thread.join(timeout=5)
    assert mgr.items[0].status == STATUS_ERROR
    assert worker.computed == []

    # 「エンコード開始」用(記録しない)も計算しない
    ref.clear()
    mgr2 = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), record_autocut=False, feature_worker=worker)
    ref.append(mgr2)
    mgr2.add_item(_file_info("feature_encode"))
    mgr2.start()
    mgr2.thread.join(timeout=5)
    assert mgr2.items[0].status == STATUS_DONE
    assert worker.computed == []


def test_skip_during_feature_computation_stops_it_and_the_item_is_still_done(settings, isolated_autocut_history):
    """変換は終わっているので、特徴の計算中のスキップは計算をやめるだけ(Done)。次の録画は計算する。"""
    ref = []
    seen_stop = []

    def on_compute(stop_event):
        if len(seen_stop) == 0:
            ref[0].skip_current()
        seen_stop.append(stop_event.is_set())

    worker = _RecordingFeatureWorker(ref, on_compute)
    mgr = BatchManager(
        settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history, feature_worker=worker,
    )
    ref.append(mgr)
    mgr.add_item(_file_info("feature_skip_a"))
    mgr.add_item(_file_info("feature_skip_b"))
    mgr.start()
    mgr.thread.join(timeout=5)
    assert seen_stop == [True, False]
    assert [item.status for item in mgr.items] == [STATUS_DONE, STATUS_DONE]


def test_cancel_during_feature_computation_keeps_the_converted_item_done(settings, isolated_autocut_history):
    ref = []
    worker = _RecordingFeatureWorker(ref, lambda stop_event: ref[0].cancel())
    mgr = BatchManager(
        settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history, feature_worker=worker,
    )
    ref.append(mgr)
    mgr.add_item(_file_info("feature_cancel_a"))
    mgr.add_item(_file_info("feature_cancel_b"))
    mgr.start()
    mgr.thread.join(timeout=5)
    assert [item.status for item in mgr.items] == [STATUS_DONE, STATUS_IDLE]
    assert mgr.items[0].output_path.is_file()  # 変換した出力は消さない


def test_start_returns_false_when_already_running(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.is_running = True
    assert mgr.start() is False


def test_start_skips_non_idle_items(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))
    mgr.items[0].status = STATUS_SKIPPED

    mgr.start()
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_SKIPPED  # 変更されない
    assert mgr.items[1].status == STATUS_DONE


# ---- 実行: エラー処理 ---------------------------------------------------

def test_error_skips_the_item_and_continues(settings):
    # エラーの項目は飛ばして次へ進む(停止しない、自動再試行もしない。design doc §8.3)
    fake = FakeFFmpegWrapper(fail_first_n_calls=1)
    mgr = BatchManager(settings, ffmpeg=fake)
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))

    mgr.start()
    mgr.thread.join(timeout=5)

    # 失敗するのは最初の1回だけなので、再試行していれば"a"はDoneになる
    assert mgr.items[0].status == STATUS_ERROR
    assert mgr.items[0].error_code == "E101"
    assert mgr.items[1].status == STATUS_DONE


# ---- stop / skip_current -------------------------------------------------

def test_stop_prevents_remaining_items_from_starting(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))

    def _on_progress(_event):
        mgr.stop()

    mgr.start(progress_callback=_on_progress)
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_DONE
    assert mgr.items[1].status == STATUS_IDLE


def test_skip_current_marks_item_skipped(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.add_item(_file_info("b"))

    # skip_current()は1件目の処理中に一度だけ呼ばれるユーザー操作を模す
    # （実際のGUIでは「スキップ」ボタンのクリックに相当し、進捗イベント
    # のたびに自動発火するものではない）。
    def _on_progress(event):
        if event.current_file == 1:
            mgr.skip_current()

    mgr.start(progress_callback=_on_progress)
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_SKIPPED
    # skip_requestedは各アイテム処理後にリセットされるため2件目は影響を受けない
    assert mgr.items[1].status == STATUS_DONE


# ---- retry_item ---------------------------------------------------------

def test_retry_item_resets_error_state(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(fail_first_n_calls=1))
    mgr.add_item(_file_info("a"))
    mgr.start()
    mgr.thread.join(timeout=5)
    assert mgr.items[0].status == STATUS_ERROR

    assert mgr.retry_item(0) is True
    assert mgr.items[0].status == STATUS_IDLE
    assert mgr.items[0].error_code is None


def test_retry_item_blocked_while_running(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.is_running = True
    assert mgr.retry_item(0) is False


def test_temp_files_default_to_output_dir_and_are_cleaned_up(settings):
    # 一時フォルダ未設定なら、OSの一時フォルダ(Linuxではメモリ上の小さな
    # tmpfsのことが多い)ではなく出力先の中に作り、処理後に片付ける。
    written = []

    class RecordingFFmpeg(FakeFFmpegWrapper):
        def run(self, args, progress_callback=None, bytes_per_frame=None):
            written.append(Path(args[-1]))
            return super().run(args, progress_callback)

    assert settings.temp_directory is None
    mgr = BatchManager(settings, ffmpeg=RecordingFFmpeg())
    mgr.add_item(_file_info("a"))
    mgr.start()
    mgr.thread.join(timeout=5)

    out_dir = settings.default_output_dir
    temp_dir = out_dir / TEMP_SUBDIR_NAME
    intermediates = [p for p in written if p != mgr.items[0].output_path]
    # 処理ごとのフォルダ(<出力先>/.ts_smartcut_temp/run_<id>/)に作る
    assert intermediates and all(p.parent.parent == temp_dir and p.parent.name.startswith("run_") for p in intermediates)
    assert not temp_dir.exists()
    assert mgr.items[0].status == STATUS_DONE


def test_stale_temp_files_are_removed_when_processing_starts(settings):
    # 異常終了で残った前回の一時ファイルは、その出力先で処理を始める時に消す (design doc §11.2)
    temp_dir = settings.default_output_dir / TEMP_SUBDIR_NAME
    stale = temp_dir / "run_crashed"
    stale.mkdir(parents=True)
    (stale / ".lock").write_bytes(b"")
    (stale / "temp_part.ts").write_bytes(b"x" * 1000)

    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("a"))
    mgr.start()
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_DONE
    assert not temp_dir.exists()


def test_missing_source_is_reported_as_file_not_found_and_next_item_runs(settings):
    # バッチに追加したあと、処理の前に元ファイルの名前を変えた・消した
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    gone = _file_info("gone")
    mgr.add_item(gone)
    mgr.add_item(_file_info("next"))
    gone.file_path.unlink()

    assert mgr.start() is True
    mgr.thread.join(timeout=5)

    first, second = mgr.items
    assert (first.status, first.error_code) == (STATUS_ERROR, "E001")
    assert "見つかりません" in first.error_message and str(gone.file_path) in first.error_message
    assert second.status == STATUS_DONE


def test_engine_gets_the_complement_of_the_keep_ranges(settings, monkeypatch):
    # file_info.cutsは残す区間。エンジンには削除する区間を渡す(2026-10-06、
    # 本編を区間に指定したらCMだけが出力された)。
    from ts_smartcut.ffmpeg.smart_cut import SmartCutEngine

    received = []

    def fake_process(self, file_info, cuts, output_path, mode="smart_cut", **kwargs):
        received.append(list(cuts))
        output_path.write_bytes(b"fake output")
        return mode, None

    monkeypatch.setattr(SmartCutEngine, "process", fake_process)
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), hardware=HardwareDetection())
    info = _file_info("keep")
    info.cuts = [CutRange(10, 40), CutRange(60, 90)]
    mgr.add_item(info)
    assert mgr.start() is True
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_DONE
    assert received == [[CutRange(0, 10), CutRange(40, 60), CutRange(90, 100)]]


def test_item_without_keep_ranges_is_an_error(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), hardware=HardwareDetection())
    info = _file_info("nothing_kept")
    info.cuts = []
    mgr.add_item(info)
    assert mgr.start() is True
    mgr.thread.join(timeout=5)

    item = mgr.items[0]
    assert (item.status, item.error_code) == (STATUS_ERROR, "E201")
    assert "残す区間がありません" in item.error_message


def test_cancel_ends_the_item_with_e504_and_removes_partial_output(settings):
    # 中止ボタン: 実行中のffmpegを終了させ、作りかけの出力を消し、残りの項目も始めない
    started = threading.Event()

    class SlowFFmpeg(FakeFFmpegWrapper):
        def run(self, args, progress_callback=None, bytes_per_frame=None):
            if self.run_call_count == 1:  # 2回目(結合)で出力を書きかけて待つ
                Path(args[-1]).write_bytes(b"partial")
                started.set()
                self.cancel_event.wait(10)
            return super().run(args, progress_callback)

    mgr = BatchManager(settings, ffmpeg=SlowFFmpeg(), hardware=HardwareDetection())
    mgr.add_item(_file_info("cancel_me"))
    mgr.add_item(_file_info("not_started"))
    assert mgr.start() is True
    assert started.wait(5)
    mgr.cancel()
    mgr.thread.join(timeout=5)

    first, second = mgr.items
    assert (first.status, first.error_code) == (STATUS_ERROR, "E504")
    assert not any((settings.default_output_dir).glob("cancel_me*"))
    assert second.status == STATUS_IDLE


def test_same_recording_with_different_output_names_is_recorded_as_two_episodes(
        settings, isolated_autocut_history, monkeypatch):
    # 1本の録画に入った2つの話を、保存名称を変えて別々に追加する(2026-10-11)。両方の記録が
    # 有効のまま残り、2つ目の特徴は2つの話の区間を合わせて計算する(1つ目の本編を「残さなかった
    # 部分」として扱わない)
    from ts_smartcut.ffmpeg.smart_cut import SmartCutEngine

    def fake_process(self, file_info, cuts, output_path, mode="smart_cut", **kwargs):
        output_path.write_bytes(b"fake output")
        return mode, None

    monkeypatch.setattr(SmartCutEngine, "process", fake_process)
    ref = []
    worker = _RecordingFeatureWorker(ref)
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), autocut_recorder=isolated_autocut_history,
                       feature_worker=worker)
    ref.append(mgr)
    out = settings.default_output_dir
    first, second = _file_info("episodes"), _file_info("episodes")
    first.cuts = [CutRange(5, 40)]
    second.cuts = [CutRange(55, 95)]
    assert mgr.add_item(first, output_path=out / "第1話.mp4")
    assert not mgr.add_item(second, output_path=out / "第1話.mp4")  # 同じ保存名称は足さない
    assert mgr.add_item(second, output_path=out / "第2話.mp4")
    mgr.start()
    mgr.thread.join(timeout=5)

    assert [item.status for item in mgr.items] == [STATUS_DONE, STATUS_DONE]
    assert (out / "第1話.mp4").is_file() and (out / "第2話.mp4").is_file()
    with isolated_autocut_history.db.get_connection() as conn:
        sessions = conn.execute("SELECT recording_id, is_active, output_name FROM cut_sessions ORDER BY id").fetchall()
    assert [(s["is_active"], s["output_name"]) for s in sessions] == [(1, "第1話.mp4"), (1, "第2話.mp4")]
    assert len({s["recording_id"] for s in sessions}) == 1
    assert [(r.start, r.end) for r in worker.computed[0].keep] == [(5, 40)]
    assert [(r.start, r.end) for r in worker.computed[1].keep] == [(5, 40), (55, 95)]


def test_named_output_is_never_overwritten(settings, isolated_autocut_history):
    # 保存名称で決めた出力先に、追加の後で同じ名前のファイルができていたら、上書きもエラーも
    # せず連番の名前で出力する(2026-10-22)。記録 DB にも実際の名前を残す
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), hardware=HardwareDetection(),
                       autocut_recorder=isolated_autocut_history)
    target = settings.default_output_dir / "taken.mp4"
    mgr.add_item(_file_info("taken"), output_path=target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"someone else's file")
    mgr.start()
    mgr.thread.join(timeout=5)

    item = mgr.items[0]
    assert item.status == STATUS_DONE and item.output_path == target.with_name("taken_001.mp4")
    assert item.output_path.read_bytes() == b"fake output"
    assert target.read_bytes() == b"someone else's file"
    with isolated_autocut_history.db.get_connection() as conn:
        names = [r["output_name"] for r in conn.execute("SELECT output_name FROM cut_sessions")]
    assert names == ["taken_001.mp4"]


def test_file_created_during_processing_is_not_overwritten(settings):
    # 変換の途中で出力先に同じ名前のファイルができた(別のアプリ等)。結合は処理のフォルダの
    # 中で行うので、そのファイルは上書きせず、こちらの出力を連番の名前で置く(2026-10-22)
    target = settings.default_output_dir / "racing.mp4"

    class InterferingFFmpeg(FakeFFmpegWrapper):
        def run(self, args, progress_callback=None, bytes_per_frame=None):
            assert Path(args[-1]) != target  # 出力先へ直接は書かない
            if not target.exists():
                target.write_bytes(b"someone else's file")
            return super().run(args, progress_callback)

    mgr = BatchManager(settings, ffmpeg=InterferingFFmpeg(), hardware=HardwareDetection(), record_autocut=False)
    mgr.add_item(_file_info("racing"), output_path=target)
    mgr.start()
    mgr.thread.join(timeout=5)

    item = mgr.items[0]
    assert item.status == STATUS_DONE and item.output_path == target.with_name("racing_001.mp4")
    assert target.read_bytes() == b"someone else's file"
    assert item.output_path.read_bytes() == b"fake output"


def test_failed_item_never_deletes_a_file_it_did_not_write(settings):
    # 失敗・中止の時に出力先の同じ名前のファイルを消していた(作りかけの出力のつもり)。
    # 作りかけは処理のフォルダの中にしか無いので、出力先には触らない(2026-10-22)
    target = settings.default_output_dir / "victim.mp4"

    class FailingFFmpeg(FakeFFmpegWrapper):
        def run(self, args, progress_callback=None, bytes_per_frame=None):
            target.write_bytes(b"someone else's file")
            raise MuxFailedError("E101", "fake ffmpeg failure")

    mgr = BatchManager(settings, ffmpeg=FailingFFmpeg(), hardware=HardwareDetection(), record_autocut=False)
    mgr.add_item(_file_info("victim"), output_path=target)
    mgr.start()
    mgr.thread.join(timeout=5)

    assert mgr.items[0].status == STATUS_ERROR
    assert target.read_bytes() == b"someone else's file"
    assert [p.name for p in settings.default_output_dir.iterdir()] == ["victim.mp4"]


def test_rename_item_only_for_unfinished_items_and_unique_names(settings):
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), hardware=HardwareDetection())
    out = settings.default_output_dir
    mgr.add_item(_file_info("rename_a"), output_path=out / "a.mp4")
    mgr.add_item(_file_info("rename_b"), output_path=out / "b.mp4")
    assert mgr.output_paths() == [out / "a.mp4", out / "b.mp4"]
    assert mgr.output_paths(exclude_index=0) == [out / "b.mp4"]

    assert not mgr.rename_item(0, out / "B.mp4" if os.name == "nt" else out / "b.mp4")  # 他の項目と同じ
    assert mgr.rename_item(0, out / "c.mp4")
    assert mgr.items[0].output_path == out / "c.mp4"
    for status, allowed in ((STATUS_ERROR, True), (STATUS_SKIPPED, True), (STATUS_DONE, False)):
        mgr.items[1].status = status
        assert mgr.can_rename(1) is allowed
    mgr.is_running = True
    try:
        assert not mgr.rename_item(0, out / "d.mp4")
    finally:
        mgr.is_running = False



# ---- スキップ(2026-11-17 の見直し) ------------------------------------------

def test_skip_while_idle_does_not_skip_the_next_batch(settings):
    # 止まっている時に押したスキップが残り、次に変換した項目が Skipped になった
    mgr = BatchManager(settings, ffmpeg=FakeFFmpegWrapper())
    mgr.add_item(_file_info("idle_skip_a"))
    mgr.skip_current()
    mgr.start()
    mgr.thread.join(timeout=60)
    assert mgr.items[0].status == STATUS_DONE


class _SkipThenFail(FakeFFmpegWrapper):
    """1 回目の ffmpeg の途中でスキップが押され、同時にその ffmpeg が失敗する。"""

    def __init__(self):
        super().__init__()
        self.manager = None

    def run(self, args, progress_callback=None, bytes_per_frame=None):
        if self.run_call_count == 0:
            self.run_call_count += 1
            self.manager.skip_current()
            raise MuxFailedError("E101", "fake ffmpeg failure")
        return super().run(args, progress_callback, bytes_per_frame)


def test_skip_on_a_failed_item_does_not_skip_the_next_item(settings):
    ffmpeg = _SkipThenFail()
    mgr = BatchManager(settings, ffmpeg=ffmpeg)
    ffmpeg.manager = mgr
    mgr.add_item(_file_info("skip_fail_a"))
    mgr.add_item(_file_info("skip_fail_b"))
    mgr.start()
    mgr.thread.join(timeout=60)
    assert [item.status for item in mgr.items] == [STATUS_ERROR, STATUS_DONE]


def test_skip_during_conversion_stops_ffmpeg_and_places_no_output(settings, isolated_autocut_history):
    # 変換中のスキップは ffmpeg を止め、出力を置かずに Skipped。次の項目の特徴の計算は止めない
    ref = []
    seen_stop = []
    worker = _RecordingFeatureWorker(ref, lambda stop_event: seen_stop.append(stop_event.is_set()))
    ffmpeg = FakeFFmpegWrapper()
    mgr = BatchManager(settings, ffmpeg=ffmpeg, autocut_recorder=isolated_autocut_history, feature_worker=worker)
    ref.append(mgr)
    mgr.add_item(_file_info("convert_skip_a"))
    mgr.add_item(_file_info("convert_skip_b"))
    first = mgr.items[0].output_path

    def on_progress(event):
        if event.current_file == 1 and not ffmpeg.cancel_event.is_set() and mgr.items[0].status == "Processing":
            mgr.skip_current()

    mgr.start(progress_callback=on_progress)
    mgr.thread.join(timeout=60)
    assert [item.status for item in mgr.items] == [STATUS_SKIPPED, STATUS_DONE]
    assert first is None or not Path(first).exists()  # 出力は置かない
    assert _recorded_count(isolated_autocut_history) == 1  # 2 件目だけ記録
    assert seen_stop == [False]  # 2 件目の特徴の計算は止まらない
