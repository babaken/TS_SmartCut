"""音声の選択 (設計書 §4.12): 番組情報の音声の一覧、選んだ音声だけの出力、プロジェクト・記録 DB・
バッチへの保存、選択の画面とプレビューの下の選択欄。

出力は、先頭 20 秒は音声 1 つ・その後に 2 つ目の音声が加わる合成の TS(GR-B の録画と同じ形。
tests/_two_audio.py)を本物の ffmpeg で切って、音の高さ(440 / 880Hz)で見分ける。
"""

import json
import subprocess
from datetime import datetime, timedelta

import numpy as np
import pytest

from tests._two_audio import FIRST_PID, SECOND_PID, make_two_audio_ts
from ts_smartcut.broadcast.audio_tracks import AudioTrack, audio_tracks, same_track
from ts_smartcut.broadcast.program_info import (
    AudioComponent,
    EventInfo,
    ProgramInfo,
    StreamInfo,
)
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.models import CutRange, FileCutInfo

MAIN = AudioComponent(component_tag=0x10, component_type=0x03, stream_type=0x0F, main=True, language="jpn")
DESCRIBED = AudioComponent(component_tag=0x11, component_type=0x03, stream_type=0x0F, main=False,
                           language="jpn", text="解説")


def _info(second=True, start=datetime(2026, 10, 1, 1, 0, 0)):
    """先頭の PMT は音声 1 つ、番組の途中の PMT で 2 つ目が加わる番組情報(GR-B と同じ形)。"""
    head = [StreamInfo(0x111, 0x02, 0x00), StreamInfo(FIRST_PID, 0x0F, 0x10)]
    middle = head + ([StreamInfo(SECOND_PID, 0x0F, 0x11)] if second else [])
    event = EventInfo(1, start + timedelta(seconds=6), timedelta(minutes=30), name="試験用の録画 A",
                      audio_components=[MAIN, DESCRIBED] if second else [MAIN])
    return ProgramInfo(service_id=1, original_network_id=0x7FE0, streams=head, programme_streams=middle,
                       event=event, events=[event], recording_start=start)


# ---- 番組情報 ------------------------------------------------------------------


def test_audio_component_descriptor_is_parsed(tmp_path):
    from tests import test_program_info as t
    from ts_smartcut.broadcast.program_info import read_program_info

    # stream_content 2, component_type 0x03, tag 0x11, stream_type 0x0F, simulcast 0xFF,
    # main 0 / quality 3 / 48kHz, "jpn", 説明(ARIB の英数字で "AD")
    described = bytes([0xF2, 0x03, 0x11, 0x0F, 0xFF, 0x3F]) + b"jpn" + t.arib("AD")
    dual = bytes([0xF2, 0x02, 0x10, 0x0F, 0xFF, 0xFF]) + b"jpneng"  # 二か国語(言語 2 つ)、主音声
    descs = t.descriptor(0x4D, b"jpn" + bytes([len(t.arib("A"))]) + t.arib("A") + b"\x00")
    descs += t.descriptor(0xC4, dual) + t.descriptor(0xC4, described)
    event_id, start, duration = t.TARGET
    ev = (bytes([event_id >> 8, event_id & 0xFF]) + t.mjd_bcd(start) + t.duration_bcd(duration)
          + bytes([0x80 | (len(descs) >> 8), len(descs) & 0xFF]) + descs)
    info = read_program_info(t._recording_with(tmp_path / "rec.ts", lambda w: w.sections(0x12, t.eit(0, ev))))
    d, c = info.events[0].audio_components
    assert (c.component_tag, c.component_type, c.stream_type, c.main, c.language, c.language2, c.text) == \
        (0x11, 0x03, 0x0F, False, "jpn", "", "AD")
    assert (d.component_tag, d.main, d.language, d.language2, d.text) == (0x10, True, "jpn", "eng", "")


def test_tracks_merge_the_head_and_programme_pmts_with_names_from_the_eit():
    tracks = audio_tracks(_info())
    assert [(t.index, t.pid, t.component_tag) for t in tracks] == [(0, FIRST_PID, 0x10), (1, SECOND_PID, 0x11)]
    assert tracks[0].label() == "1. 音声 1  ステレオ  日本語"  # 説明の無い主音声は番号
    assert tracks[1].label() == "2. 解説  ステレオ  日本語"
    assert audio_tracks(_info(second=False))[0].pid == FIRST_PID and len(audio_tracks(_info(second=False))) == 1
    assert audio_tracks(None) == []
    again = AudioTrack.from_dict(tracks[1].to_dict())
    assert again == tracks[1] and same_track(again, tracks[1]) and not same_track(tracks[0], tracks[1])
    # PID が入れ替わっても component_tag が同じなら同じ音声
    assert same_track(AudioTrack(0, 0x200, 0x10), tracks[0])


# ---- 出力 ------------------------------------------------------------------


@pytest.fixture(scope="module")
def two_audio_ts(tmp_path_factory):
    return make_two_audio_ts(tmp_path_factory.mktemp("two_audio") / "two_audio.ts")


def _peak_hz(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32)[:8000 * 5]
    return float(np.argmax(np.abs(np.fft.rfft(x))) * 8000 / len(x))


def _audio_streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return [s for s in json.loads(out)["streams"] if s["codec_type"] == "audio"]


@pytest.mark.parametrize("mode", ["smart_cut", "simple_copy"])
@pytest.mark.parametrize("index,pid,hz", [(0, FIRST_PID, 440), (1, SECOND_PID, 880)])
def test_output_has_only_the_chosen_audio(two_audio_ts, tmp_path, mode, index, pid, hz):
    from ts_smartcut.core.hardware_encoders import HardwareDetection
    from ts_smartcut.ffmpeg.smart_cut import SmartCutEngine
    from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper
    from ts_smartcut.models import AppSettings
    from ts_smartcut.system.temp_file_manager import TempFileManager

    settings = AppSettings()
    ffmpeg = FFmpegWrapper(settings)
    info = ffmpeg.get_file_info(two_audio_ts)
    info.audio_track = {"index": index, "pid": pid, "component_tag": 0x10 + index}
    fps = info.fps
    # 2 つ目の音声が加わった後(22〜40 秒)を残す
    removed = [CutRange(0, int(22 * fps)), CutRange(int(40 * fps), info.total_frames)]
    out = tmp_path / "out.ts"
    engine = SmartCutEngine(ffmpeg, TempFileManager(tmp_path / "work"), settings, HardwareDetection())
    engine.process(info, removed, out, mode=mode)
    assert len(_audio_streams(out)) == 1
    assert _peak_hz(out) == pytest.approx(hz, abs=5)


# ---- プロジェクト・記録 DB・バッチ ------------------------------------------------------


def _file_info(path="a.ts"):
    from pathlib import Path

    return FileCutInfo(Path(path), 3000, 29.97, (1920, 1080), "mpeg2video", "aac", 1, 100.0, 1,
                       cuts=[CutRange(0, 100)])


def test_project_keeps_the_audio_and_8_2_files_still_open(tmp_path):
    from ts_smartcut.core.project_manager import CURRENT_PROJECT_VERSION, ProjectManager
    from ts_smartcut.models import AppSettings, Project

    tracks = audio_tracks(_info())
    info = _file_info()
    info.audio_tracks = [t.to_dict() for t in tracks]
    info.audio_track = tracks[1].to_dict()
    now = datetime(2026, 10, 26)
    path = tmp_path / "p.tsproj"
    ProjectManager.save_project(Project(CURRENT_PROJECT_VERSION, info, AppSettings(), now, now), path)
    loaded = ProjectManager.load_project(path)
    assert CURRENT_PROJECT_VERSION == "8.3"
    assert loaded.file_info.audio_track == tracks[1].to_dict() and loaded.file_info.selected_audio_index() == 1
    assert len(loaded.file_info.audio_tracks) == 2

    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = "8.2"
    del data["file_info"]["audio_track"], data["file_info"]["audio_tracks"]
    path.write_text(json.dumps(data), encoding="utf-8")
    old = ProjectManager.load_project(path)
    assert old.file_info.audio_track is None and old.file_info.selected_audio_index() == 0


def test_the_cut_record_keeps_the_chosen_audio(tmp_path, interlaced_sample_video):
    from ts_smartcut.autocut.history_db import CutHistoryDB
    from ts_smartcut.autocut.recorder import AutoCutRecorder

    db = CutHistoryDB(tmp_path / "cut_history.db")
    recorder = AutoCutRecorder(db)
    info = _file_info(interlaced_sample_video)
    tracks = audio_tracks(_info())
    info.audio_tracks = [t.to_dict() for t in tracks]
    info.audio_track = tracks[1].to_dict()
    session = recorder.record_batch_done(info, "smart_cut", program_info=_info(), output_name="a_cut.ts")
    second = _file_info(interlaced_sample_video)
    second_session = recorder.record_batch_done(second, "smart_cut", output_name="b_cut.ts")
    with db.get_connection() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM cut_sessions")}
    assert (rows[session]["audio_index"], rows[session]["audio_pid"], rows[session]["audio_component_tag"],
            rows[session]["audio_component_type"], rows[session]["audio_text"]) == (1, SECOND_PID, 0x11, 0x03, "解説")
    assert rows[second_session]["audio_index"] is None  # 選んでいない(最初の音声・記録なし)


def test_batch_items_change_their_audio_only_while_waiting(tmp_path):
    from ts_smartcut.core.batch_manager import STATUS_DONE, BatchManager
    from ts_smartcut.core.hardware_encoders import HardwareDetection
    from ts_smartcut.models import AppSettings

    manager = BatchManager(AppSettings(), hardware=HardwareDetection(), record_autocut=False)
    info = _file_info(tmp_path / "a.ts")
    tracks = audio_tracks(_info())
    info.audio_tracks = [t.to_dict() for t in tracks]
    info.audio_track = tracks[0].to_dict()
    manager.add_item(info, output_path=tmp_path / "a_cut.ts")
    assert manager.set_audio_track(0, tracks[1].to_dict())
    assert manager.items[0].file_info.selected_audio_index() == 1
    assert not manager.set_audio_track(0, AudioTrack(5, 0x999).to_dict())  # 一覧に無い音声
    manager.items[0].status = STATUS_DONE
    assert not manager.set_audio_track(0, tracks[0].to_dict())


# ---- 画面 ------------------------------------------------------------------


class FakePreview:
    def __init__(self):
        self.calls = []
        self.frame = 0

    def seek_frame(self, frame):
        self.frame = frame
        self.calls.append(("seek", frame))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def set_audio_pid(self, pid):
        self.calls.append(("pid", pid))

    def current_frame(self):
        return self.frame


def test_dialog_switches_the_preview_audio_and_returns_the_choice(qapp):
    from PySide6.QtWidgets import QDialog

    from ts_smartcut.gui.audio_select_dialog import AudioSelectDialog

    tracks = audio_tracks(_info())
    preview = FakePreview()
    dialog = AudioSelectDialog(tracks, 3000, 29.97, 480, preview)
    assert [r.text() for r in dialog.radios] == [t.label() for t in tracks]
    assert dialog.radios[0].isChecked()
    assert ("pid", FIRST_PID) in preview.calls and ("seek", 480) in preview.calls
    assert dialog.time_label.text() == "00:00:16"

    dialog.play()
    assert ("play",) in preview.calls and dialog.stop_button.isEnabled()
    dialog.radios[1].setChecked(True)  # 再生したまま切り替える
    assert preview.calls[-1] == ("pid", SECOND_PID)
    preview.frame = 900
    dialog._poll()
    assert dialog.slider.value() == 900 and preview.calls[-1] != ("seek", 900)  # 位置を読むだけ
    dialog.slider.setValue(1500)  # 聞く位置を動かす
    assert preview.calls[-1] == ("seek", 1500)

    dialog.accept()
    assert preview.calls[-1] == ("pause",)  # 閉じたら止める
    assert dialog.selected_track() == tracks[1]

    cancelled = AudioSelectDialog(tracks, 3000, 29.97, 0, FakePreview())
    cancelled.radios[1].setChecked(True)
    cancelled.reject()
    assert cancelled.selected_track() == tracks[0]  # キャンセルは 1 つ目


@pytest.fixture
def opened(window, interlaced_sample_video, monkeypatch, qapp):
    from PySide6.QtWidgets import QMessageBox

    from tests._qt_helpers import pump_events

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    arrived = []
    window._program_info_bridge.ready.connect(lambda *_: arrived.append(1))
    window.open_file(interlaced_sample_video)
    # 開いた時の本物の番組情報の読み込み(この動画には無い)が届くのを待つ(後から届いて上書きしないように)
    assert pump_events(qapp, lambda: bool(arrived) and not window._loading, timeout_ms=10000)
    window.batch_manager.items.clear()
    yield window
    window.batch_manager.items.clear()


def test_window_asks_when_there_are_two_audio_tracks_and_uses_the_choice(opened, monkeypatch, qapp):
    from tests._qt_helpers import pump_events
    from ts_smartcut.gui import main_window as mw

    window = opened
    assert not window.audio_combo.isEnabled()  # 番組情報の無い動画
    shown = []

    def fake_exec(dialog):
        shown.append([r.text() for r in dialog.radios])
        dialog.radios[1].setChecked(True)
        dialog.accept()
        return 1

    monkeypatch.setattr(mw.AudioSelectDialog, "exec", fake_exec)
    window._on_program_info_ready(window._program_info_request, _info())
    pump_events(qapp, lambda: bool(shown), timeout_ms=2000)
    assert shown and len(shown[0]) == 2
    assert window.file_info.selected_audio_index() == 1 and window.file_info.audio_track["pid"] == SECOND_PID
    assert window.audio_combo.isEnabled() and window.audio_combo.currentIndex() == 1

    # プレビューの下の選択欄で選び直す
    window.audio_combo.setCurrentIndex(0)
    assert window.file_info.selected_audio_index() == 0

    # 音声が 1 つなら聞かない
    shown.clear()
    window._on_program_info_ready(window._program_info_request, _info(second=False))
    qapp.processEvents()
    assert not shown and not window.audio_combo.isEnabled()
    assert window.file_info.audio_track["pid"] == FIRST_PID


def test_project_audio_is_used_without_asking(opened, monkeypatch, qapp):
    from ts_smartcut.gui import main_window as mw

    window = opened
    asked = []
    monkeypatch.setattr(mw.AudioSelectDialog, "exec", lambda dialog: asked.append(1) or 0)
    window._preset_audio_track = audio_tracks(_info())[1].to_dict()
    window._on_program_info_ready(window._program_info_request, _info())
    qapp.processEvents()
    assert not asked
    assert window.file_info.selected_audio_index() == 1 and window.audio_combo.currentIndex() == 1


def test_batch_list_menu_changes_the_item_audio(opened):
    window = opened
    tracks = audio_tracks(_info())
    info = window.file_info
    info.audio_tracks = [t.to_dict() for t in tracks]
    info.audio_track = tracks[0].to_dict()
    window.cut_manager.add_cut(0, 30)
    window.batch_manager.add_item(info, output_path=info.file_path.with_name("x_cut.ts"))
    view = window.batch_list_view
    menu = view.item_menu(0)
    # 右クリックには「出力形式」のメニューもある(設計書 §6.9)ので、題で選ぶ
    audio_menu = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == tr("batch.audio_menu"))
    actions = audio_menu.actions()
    assert [a.text() for a in actions] == [t.label() for t in tracks] and actions[0].isChecked()
    actions[1].trigger()
    assert window.batch_manager.items[0].file_info.selected_audio_index() == 1


def test_closing_after_choosing_an_audio_track_does_not_crash(two_audio_ts, tmp_path):
    """音声を選んだ後に閉じると、mpv を終わらせている途中に track-list の知らせが閉じかけのハンドルを読んで
    落ちた(Linux で毎回、2026-10-27)。落ちるとプロセスごと終わるので、子プロセスで動かして終了コードを見る。"""
    import os
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), os.environ.get("PYTHONPATH", "")]))
    for k in range(3):
        config = tmp_path / f"config{k}"
        config.mkdir()
        result = subprocess.run([sys.executable, str(root / "tests" / "_close_after_audio_choice.py"),
                                 str(two_audio_ts), str(config)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=env, timeout=120)
        assert result.returncode == 0 and "closed" in result.stdout, (k, result.returncode, result.stderr[-2000:])


class _ClosingMpv:
    """terminate の途中で track-list の知らせを出す偽の mpv(本物は終わる時に一覧が変わったと知らせる)。
    閉じかけの後に一覧を読まれたら記録する。"""

    def __init__(self):
        self.observers = []
        self.terminating = False
        self.read_while_terminating = 0
        self.aid = "auto"

    def unobserve_property(self, name, handler):
        self.observers = [(n, h) for n, h in self.observers if not (n == name and h == handler)]

    @property
    def track_list(self):
        if self.terminating:
            self.read_while_terminating += 1
        return [{"type": "audio", "id": 2, "src-id": SECOND_PID}]

    def terminate(self):
        self.terminating = True
        for name, handler in list(self.observers):
            handler(name, None)


def test_track_list_notices_do_not_touch_mpv_while_closing():
    from ts_smartcut.mpv.controller import MPVController

    controller = MPVController()
    fake = _ClosingMpv()
    controller._mpv = fake
    fake.observers.append(("track-list", controller._on_track_list))
    controller._audio_pid = SECOND_PID
    controller._on_track_list("track-list", None)  # 開いている間は選び直す
    assert fake.aid == 2
    # 監視を外せなかった時も(知らせが残っていても)、閉じかけなら読まない
    fake.unobserve_property = lambda *a: None
    controller.close()
    assert fake.read_while_terminating == 0
