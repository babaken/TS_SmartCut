"""自動カットの案の画面 (設計書 §31.6・§4.11、計画 A6): 青の行・承認・棄却・すべて承認・Undo、
タイムラインと音声波形の帯、バッチ追加の確認、区間のクリックで先頭へ移動、読み込みのポップアップ
(実際の Qt の画面)。"""

import pytest
from PySide6.QtWidgets import QMessageBox

from ts_smartcut.models import PROPOSAL_ACCEPTED, CutProposal


@pytest.fixture
def opened(window, sample_video, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.open_file(sample_video)
    window.batch_manager.items.clear()
    yield window
    window.batch_manager.items.clear()


def _show(window, proposals):
    window._on_proposals_ready(window._program_info_request, proposals, "")


def _proposals():
    return [CutProposal(0, 20, 0.9, "本編 1秒: ロゴあり(0.80) / 後: ロゴなし(0.02)・15秒(15秒×1)"),
            CutProposal(40, 70, 0.4, "本編 1秒: CM らしくない(1.2秒)")]


def test_proposals_are_listed_in_blue_with_reason_and_confidence(opened):
    window = opened
    _show(window, _proposals())
    assert window.cut_list.topLevelItemCount() == 2
    item = window.cut_list.topLevelItem(0)
    assert item.text(0) == "0" and item.text(1) == "20"
    assert "案（確信度 0.9）" in item.text(2) and "ロゴあり" in item.text(2)
    assert "ロゴなし" in item.toolTip(2)
    assert item.background(0).color().name() == "#1e88e5"
    assert window.proposal_status_label.text() == "自動カットの案 2 件"
    assert window.timeline._proposals == [(0, 20), (40, 70)]
    assert window.waveform_view._proposals == [(0, 20), (40, 70)]
    assert window.accept_all_proposals_button.isEnabled()
    assert not window.accept_proposal_button.isEnabled()  # 案を選ぶまで


def test_accept_reject_accept_all_and_undo(opened):
    window = opened
    _show(window, _proposals())
    first, second = window.cut_manager.proposals

    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    assert window.accept_proposal_button.isEnabled() and window._selected_cut_index is None
    window.accept_proposal_button.click()
    assert [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts] == [(0, 20)]
    assert window.cut_list.topLevelItemCount() == 2  # 残す区間 1 + 案 1
    assert window.timeline._proposals == [(40, 70)]

    window._on_undo()
    assert window.cut_manager.cuts == [] and window.cut_list.topLevelItemCount() == 2
    assert window.timeline._proposals == [(0, 20), (40, 70)]

    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(1))
    window.reject_proposal_button.click()
    assert window.cut_manager.pending_proposals == [first]
    assert window.cut_list.topLevelItemCount() == 1

    window.accept_all_proposals_button.click()
    assert [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts] == [(0, 20)]
    assert first.status == PROPOSAL_ACCEPTED and not window.accept_all_proposals_button.isEnabled()
    events = [e["event_type"] for e in window.cut_manager.edit_events]
    assert "proposal_rejected" in events and "proposal_accepted" in events


def test_add_to_batch_asks_while_proposals_are_undecided(opened, monkeypatch):
    window = opened
    _show(window, _proposals())
    window.cut_manager.accept_proposal(window.cut_manager.proposals[0])
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda parent, title, text: asked.append(text) or QMessageBox.StandardButton.No)
    window._on_add_to_batch()
    assert asked and "未判断の自動カットの案が 1 件" in asked[0]
    assert window.batch_manager.items == []

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._on_add_to_batch()
    item = window.batch_manager.items[-1]
    assert [(p["start_frame"], p["status"]) for p in item.proposals] == [(0, "accepted"), (40, "pending")]


def test_results_for_an_earlier_file_are_ignored(opened):
    window = opened
    window._on_proposals_ready(window._program_info_request - 1, _proposals(), "")
    assert window.cut_manager.proposals == []
    window._on_proposals_ready(window._program_info_request, None, "自動カットの案を作れませんでした")
    assert window.proposal_status_label.text() == "自動カットの案を作れませんでした"


def test_proposal_preparation_stops_when_a_batch_runs(opened):
    from ts_smartcut.gui.main_window import _ProposalStop

    window = opened
    stop = _ProposalStop(window)
    assert not stop.is_set()
    window.batch_manager.is_running = True
    try:
        assert stop.is_set()
    finally:
        window.batch_manager.is_running = False
    stop.set()
    assert stop.is_set()


def test_clicking_a_range_or_a_proposal_moves_to_its_first_frame(opened):
    window = opened
    window.cut_manager.add_cut(10, 20)
    _show(window, [CutProposal(40, 70, 0.4, "案")])
    for row, frame in ((0, 10), (1, 40)):
        item = window.cut_list.topLevelItem(row)
        window.cut_list.itemClicked.emit(item, 0)
        assert window.seek_slider.value() == frame
        assert window.timeline._current_frame == frame


# ---- 読み込みのポップアップ (§4.11) ---------------------------------------------

import threading  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from tests._qt_helpers import pump_events  # noqa: E402
from ts_smartcut.broadcast.program_info import JST, EventInfo, ProgramInfo  # noqa: E402
from ts_smartcut.gui import main_window as main_window_module  # noqa: E402


@pytest.fixture
def loading(window, sample_video, monkeypatch, qapp):
    """録画を開くと案を作る(番組情報と案の準備は差し替え。案は release() まで出ない)。"""
    start = datetime(2026, 9, 30, 22, 0, tzinfo=JST)
    info = ProgramInfo(service_id=1, service_name="局", recording_start=start,
                       recording_end=start + timedelta(minutes=30),
                       event=EventInfo(1, start, timedelta(minutes=30), name="番組"))
    release = threading.Event()
    calls = []

    def fake_build(db, path, fps, program_info, registration, stop_event=None, progress=None, **kwargs):
        calls.append(stop_event)
        progress(10)
        while not release.wait(0.02):
            if stop_event.is_set():
                return None
        return [CutProposal(0, 20, 0.9, "本編")]

    monkeypatch.setattr(main_window_module, "read_program_info", lambda path: info)
    monkeypatch.setattr(main_window_module, "build_file_proposals", fake_build)
    monkeypatch.setattr(window, "autocut_proposals", True)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    yield window, release, calls, info
    release.set()
    pump_events(qapp, lambda: not window._loading, timeout_ms=3000)
    window._end_loading()


def _locked(window):
    return not window.centralWidget().isEnabled() and not window.menuBar().isEnabled()


def test_loading_popup_locks_the_window_until_the_proposals_arrive(loading, sample_video, qapp):
    window, release, calls, _info = loading
    assert window.open_file(sample_video)
    assert window._loading and _locked(window) and window._loading_dialog.isVisible()
    pump_events(qapp, lambda: window._loading_dialog.stage == "proposals")
    assert window._loading_dialog.skip_button.isVisible()
    pump_events(qapp, lambda: "%" in window._loading_dialog.label.text())
    assert _locked(window)

    release.set()
    pump_events(qapp, lambda: not window._loading)
    assert not _locked(window) and not window._loading_dialog.isVisible()
    assert [(p.start_frame, p.end_frame) for p in window.cut_manager.pending_proposals] == [(0, 20)]


def test_skip_unlocks_and_drops_the_proposals(loading, sample_video, qapp):
    window, release, calls, _info = loading
    window.open_file(sample_video)
    pump_events(qapp, lambda: window._loading_dialog.stage == "proposals")
    window._loading_dialog.skip_button.click()
    assert not window._loading and not _locked(window)
    assert window.proposal_status_label.text() == "自動カットの案の準備をやめました"
    pump_events(qapp, lambda: calls and calls[0].is_set())
    pump_events(qapp, lambda: False, timeout_ms=200)
    assert window.cut_manager.proposals == []


def test_recording_without_program_info_unlocks_after_reading(loading, sample_video, qapp, monkeypatch):
    window, release, calls, _info = loading
    monkeypatch.setattr(main_window_module, "read_program_info", lambda path: None)
    window.open_file(sample_video)
    pump_events(qapp, lambda: not window._loading)
    assert not _locked(window) and calls == []


def test_no_proposals_while_a_batch_runs(loading, sample_video, qapp):
    window, release, calls, _info = loading
    window.batch_manager.is_running = True
    try:
        window.open_file(sample_video)
        assert not window._loading
        assert window.proposal_status_label.text() == "バッチ処理中のため自動カットの案は作りません"
    finally:
        window.batch_manager.is_running = False
    pump_events(qapp, lambda: False, timeout_ms=300)
    assert calls == []


def test_batch_progress_shows_the_elapsed_time(opened):
    """バッチの進み具合に経過時間を出す(残り時間は出さない、§8.4)。"""
    from ts_smartcut.models import ProcessProgress

    window = opened
    view = window.batch_list_view
    view._on_progress(ProcessProgress(1, 2, "Body", 40.0, 3725.4, 99.0))
    assert view.progress_label.text() == "1/2  Body  40%  経過 1:02:05"
    view._on_progress(ProcessProgress(2, 2, "Features", 10.0, 59.0, 0.0))
    assert view.progress_label.text() == "2/2  自動カットの特徴の計算  10%  経過 0:00:59"



def _three_proposals():
    return [CutProposal(0, 15, 0.9, "案 A"), CutProposal(30, 40, 0.5, "案 B"), CutProposal(55, 70, 0.4, "案 C")]


def test_accepted_proposal_stays_selected_as_its_kept_range(opened, qapp):
    """承認した案は残す区間の行になっても選んだまま、位置は動かさない(2026-10-17、利用者の指摘:
    承認すると選択が外れ、続けて S / E で直せなかった)。並びの途中に入っても同じ区間を選ぶ。"""
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    window.cut_manager.add_cut(20, 25)
    _show(window, _three_proposals())
    window._jump_to_frame(10)
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(1))  # 案 A (0-15)
    window.accept_proposal_button.click()
    assert [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts] == [(0, 15), (20, 25)]
    assert window._selected_cut_index == 0 and window.cut_list.currentItem().text(0) == "0"
    assert window.timeline._selected_cut_index == 0 and window.jump_start_button.isEnabled()
    assert window.seek_slider.value() == 10


def test_reject_moves_to_the_next_proposal_or_stays(opened):
    """棄却すると次の案を選んでその開始へ。次の案が無ければ何も選ばず、位置もそのまま(2026-10-17)。"""
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    _show(window, _three_proposals())
    window._jump_to_frame(5)
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(1))  # 案 B
    window.reject_proposal_button.click()
    assert window._selected_proposal() is window.cut_manager.proposals[2]  # 案 C
    assert window.seek_slider.value() == 55

    window.reject_proposal_button.click()  # 案 C: 後ろに案が無い
    assert window._selected_proposal() is None and window._selected_cut_index is None
    assert window.seek_slider.value() == 55


def test_accept_all_moves_to_the_end_of_the_last_accepted_proposal(opened, monkeypatch):
    """すべて承認すると、承認したうちいちばん後ろの区間を選び、その最後のフレームへ(2026-10-17)。
    重なって承認できなかった案は数えない。"""
    window = opened
    window.cut_manager.clear_all(window.bookmark_manager)
    _show(window, _three_proposals())
    window.cut_manager.add_cut(66, 72)  # 案 C (55-70) と重なる
    window._refresh_cut_list()
    window._jump_to_frame(0)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    window.accept_all_proposals_button.click()
    assert len(warned) == 1  # 重なった 1 件
    assert [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts] == [(0, 15), (30, 40), (66, 72)]
    assert window._selected_cut_index == 1
    assert window.seek_slider.value() == 40
