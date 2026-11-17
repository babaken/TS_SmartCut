"""MainWindow経由のバッチ処理end-to-endテスト。実ffmpeg/実mpvを使う。"""

import time

import pytest

from tests._qt_helpers import pump_events
from ts_smartcut.models import AppSettings


@pytest.fixture(scope="module")
def window(qapp, tmp_path_factory):
    from ts_smartcut.gui.main_window import MainWindow

    out_dir = tmp_path_factory.mktemp("batch_integration_out")
    settings = AppSettings(
        default_output_dir=out_dir
    )
    w = MainWindow(settings)
    w.show()
    pump_events(qapp, lambda: w.preview_widget.is_connected, timeout_ms=3000)
    yield w
    w.close()
    w.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def test_add_to_batch_and_process_end_to_end(window, qapp, interlaced_sample_video, monkeypatch):
    # バッチが終わったら、特徴を保存した録画の局でしきい値の自動補正を始める(設計書 §31.9、2026-10-15)
    tune_calls = []
    monkeypatch.setattr(window.auto_tuner, "start", lambda keys=(): tune_calls.append(list(keys)) or True)
    window.open_file(interlaced_sample_video)
    pump_events(qapp, lambda: window.preview_widget.get_current_frame() is not None, timeout_ms=3000)

    window.cut_manager.add_cut(30, 45)
    window._refresh_cut_list()

    window._on_add_to_batch()

    assert len(window.batch_manager.items) == 1
    # 追加してもタブは切り替えない(編集を続けられる)。タブの名前に件数
    assert window.lists_tabs.currentWidget() is not window.batch_list_view
    qapp.processEvents()
    assert window.lists_tabs.tabText(window.lists_tabs.indexOf(window.batch_list_view)).startswith("バッチリスト (1")

    window._on_start_batch()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and window.batch_manager.is_running:
        qapp.processEvents()
        time.sleep(0.05)

    item = window.batch_manager.items[0]
    # 全テストの中でまれに失敗した(2026-10-07、単独や続けて8回では再現せず)。次に起きた時に
    # 原因(時間切れで処理中のままか、エラーか)が分かるよう状態とエラーを出す
    assert item.status == "Done", (item.status, item.error_code, item.error_message, window.batch_manager.is_running)
    assert item.output_path is not None
    assert item.output_path.exists()
    qapp.processEvents()
    assert tune_calls == []  # この MP4 は特徴を計算できない → 記録が増えていないので補正しない
    with window.feature_worker._lock:
        window.feature_worker._saved.append(999)  # 特徴を保存した録画(DB に無い録画 = 局が分からない)
    window.batch_list_view.batch_finished.emit()
    assert tune_calls == [[]]  # 全局共通だけ
    assert window.feature_worker.take_saved_recordings() == []  # 補正に渡した

    from ts_smartcut.autocut.auto_tuning import AutoTuneOutcome

    window._auto_tune_bridge.adopted.emit([AutoTuneOutcome("", None, True), AutoTuneOutcome("4:999", None, True)])
    qapp.processEvents()
    assert window.status_bar.currentMessage() == (
        "しきい値を自動補正しました（全局共通、4:999）。次に開いた録画の案から使います。")


def test_add_to_batch_without_open_file_shows_message(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ts_smartcut.gui.main_window import MainWindow

    infos = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a))

    w = MainWindow()
    w.show()
    try:
        w._on_add_to_batch()
        assert len(infos) == 1
    finally:
        w.close()
        w.deleteLater()
        qapp.processEvents()
        qapp.processEvents()
