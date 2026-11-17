import pytest

from tests.test_batch_manager import FakeFFmpegWrapper, _file_info
from ts_smartcut.core.batch_manager import BatchManager
from ts_smartcut.ffmpeg.gpu import HardwareDetection
from ts_smartcut.gui.batch_list_view import BatchListView
from ts_smartcut.i18n.language_manager import tr
from ts_smartcut.models import AppSettings, ProcessProgress


@pytest.fixture
def batch_manager(tmp_path) -> BatchManager:
    settings = AppSettings(default_output_dir=tmp_path / "out")
    # ffmpegは偽物なので、GPUの判定(GPUのあるPCでは数秒かかる)はしない
    return BatchManager(settings, ffmpeg=FakeFFmpegWrapper(), hardware=HardwareDetection())


def test_add_item_refreshes_tree(qapp, batch_manager):
    view = BatchListView(batch_manager)
    added = view.add_item(_file_info("a"))
    assert added is True
    assert view.tree.topLevelItemCount() == 1
    assert view.tree.topLevelItem(0).text(1) == "a.mp4"
    assert view.tree.topLevelItem(0).text(4) == "Smart Cut"


def test_add_duplicate_item_returns_false(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    assert view.add_item(_file_info("a")) is False
    assert view.tree.topLevelItemCount() == 1


def _wait_for_batch(batch_manager):
    # 5 秒では重い時に間に合わず、終わる前の表示を見て失敗した(2026-11-16)
    batch_manager.thread.join(timeout=60)
    assert not batch_manager.thread.is_alive(), "the batch did not finish"


def test_start_processes_and_status_reflected(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))

    view.start()
    _wait_for_batch(batch_manager)
    view._refresh_list()  # ポーリングタイマーを待たず即時反映して確認

    assert view.tree.topLevelItem(0).text(5) == "Done"


def test_progress_bridge_updates_progress_bar(qapp, batch_manager):
    view = BatchListView(batch_manager)
    progress = ProcessProgress(
        current_file=1, total_files=2, current_phase="Body",
        progress_percent=42.0, elapsed_time=1.0, estimated_remaining=1.0,
    )

    view._on_progress(progress)

    assert view.progress_bar.value() == 42
    assert "Body" in view.progress_label.text()


def test_remove_selected_item(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    view.add_item(_file_info("b"))
    view.tree.setCurrentItem(view.tree.topLevelItem(0))

    view._on_remove_selected()

    assert len(batch_manager.items) == 1
    assert batch_manager.items[0].file_info.file_path.name == "b.mp4"


def test_item_moved_reorders_batch_manager(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    view.add_item(_file_info("b"))
    view.add_item(_file_info("c"))

    view._on_item_moved(0, 2)

    assert [i.file_info.file_path.name for i in batch_manager.items] == ["b.mp4", "c.mp4", "a.mp4"]
    assert view.tree.topLevelItem(2).text(1) == "a.mp4"


def test_item_moved_while_running_reverts_visual_order(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    view.add_item(_file_info("b"))
    batch_manager.is_running = True  # 実行中を模擬

    view._on_item_moved(0, 1)

    # reorder()は実行中はFalseを返しmanager側の順序は変わらないため、
    # _refresh_list()で元の順序に描画が揃うはず。
    assert [i.file_info.file_path.name for i in batch_manager.items] == ["a.mp4", "b.mp4"]
    batch_manager.is_running = False


def test_retry_selected_item_resets_error_state(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    batch_manager.items[0].status = "Error"
    batch_manager.items[0].error_code = "E101"
    view.tree.setCurrentItem(view.tree.topLevelItem(0))

    view._on_retry_selected()

    assert batch_manager.items[0].status == "Idle"
    assert batch_manager.items[0].error_code is None


# ---- 項目ごとの処理モード (design doc §6.1) -----------------------------------------

def test_mode_menu_changes_the_items_mode(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    menu = view.mode_menu(0)
    labels = [a.text() for a in menu.actions()]
    assert labels == ["Smart Cut", "Simple Copy", "Full Re-encode"]
    assert [a.isChecked() for a in menu.actions()] == [True, False, False]

    menu.actions()[2].trigger()
    assert batch_manager.items[0].processing_mode == "full_reencode"
    assert view.tree.topLevelItem(0).text(4) == "Full Re-encode"


def test_mode_can_change_only_for_idle_or_error_items(qapp, batch_manager):
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    for status, editable in [("Idle", True), ("Error", True), ("Done", False), ("Skipped", False)]:
        batch_manager.items[0].status = status
        assert all(a.isEnabled() == editable for a in view.mode_menu(0).actions())
        assert batch_manager.set_processing_mode(0, "simple_copy") is editable
        batch_manager.items[0].processing_mode = "smart_cut"


def test_set_processing_mode_rejects_unknown_mode_and_running(batch_manager):
    batch_manager.add_item(_file_info("a"))
    assert batch_manager.set_processing_mode(0, "transcode") is False
    batch_manager.is_running = True
    try:
        assert batch_manager.set_processing_mode(0, "simple_copy") is False
    finally:
        batch_manager.is_running = False


def test_columns_can_be_resized_and_report_their_widths(qapp, batch_manager):
    # どの列もドラッグで幅を変えられ、変えた幅を知らせる(設定に覚える、2026-10-11)。
    # 長い録画名の全文はツールチップ
    from PySide6.QtWidgets import QHeaderView

    COLUMN_FILE = 1  # 順序, ファイル, 保存名称, 再生時間, モード, 状態, エラー
    view = BatchListView(batch_manager)
    view.resize(900, 400)
    view.add_item(_file_info("a"))
    view.show()
    qapp.processEvents()
    header = view.tree.header()
    assert all(header.sectionResizeMode(c) == QHeaderView.ResizeMode.Interactive for c in range(header.count()))
    assert view.tree.columnWidth(COLUMN_FILE) > 300
    assert view.tree.topLevelItem(0).toolTip(COLUMN_FILE) == str(batch_manager.items[0].file_info.file_path)

    reported = []
    view.column_widths_changed.connect(reported.append)
    header.resizeSection(COLUMN_FILE, 250)
    assert reported and reported[-1][COLUMN_FILE] == 250

    other = BatchListView(batch_manager)
    other.set_column_widths([60, 333, 222, 90, 120, 100, 200])
    assert other.column_widths()[:6] == [60, 333, 222, 90, 120, 100]
    other.set_column_widths([1, 2])  # 列の数が合わない値(保存名称の列が無かった頃の 5 列など)は使わない
    assert other.column_widths()[:6] == [60, 333, 222, 90, 120, 100]
    other.set_column_widths([61, 334, 223, 121, 101, 201])  # 再生時間の列が無かった頃の 6 列: その列は初期値
    assert other.column_widths()[:6] == [61, 334, 223, 80, 121, 101]
    view.close()


def test_clear_removes_only_the_done_items(qapp, batch_manager):
    # 完了した項目だけを一覧から消す。エラー・スキップ・未処理は残す。完了が無い時・実行中は押せない
    view = BatchListView(batch_manager)
    for name in ("a", "b", "c", "d"):
        view.add_item(_file_info(name))
    view._refresh_list()
    assert not view.clear_done_button.isEnabled()
    for item, status in zip(batch_manager.items, ("Done", "Error", "Done", "Skipped")):
        item.status = status
    view._refresh_list()
    assert view.clear_done_button.isEnabled()

    assert view.clear_done() == 2
    assert [i.file_info.file_path.stem for i in batch_manager.items] == ["b", "d"]
    assert [i.order for i in batch_manager.items] == [0, 1]  # 順序は詰め直す
    assert view.tree.topLevelItemCount() == 2
    assert not view.clear_done_button.isEnabled()
    assert view.title() == "バッチリスト (2)"
    # 同じ録画をもう一度追加できる
    assert view.add_item(_file_info("a")) is True


def test_title_shows_the_count_and_progress(qapp, batch_manager):
    view = BatchListView(batch_manager)
    titles = []
    view.title_changed.connect(titles.append)
    assert view.title() == "バッチリスト"
    view.add_item(_file_info("a"))
    view.add_item(_file_info("b"))
    assert titles[-1] == "バッチリスト (2)"
    view.start()
    assert view.title().startswith("バッチリスト (") and "/2)" in view.title()
    _wait_for_batch(batch_manager)
    view._refresh_list()
    assert titles[-1] == "バッチリスト (2)"


def test_refresh_keeps_the_scroll_position(qapp, batch_manager):
    # 0.3秒ごとの更新で全部を作り直していたため、スクロールしても先頭に戻され、
    # 長いリストの下の方を見られなかった(2026-10-10)
    view = BatchListView(batch_manager)
    view.resize(500, 200)
    view.show()
    for i in range(40):
        view.add_item(_file_info(f"rec{i:02d}"))
    qapp.processEvents()
    bar = view.tree.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    batch_manager.items[39].status = "Done"
    view._refresh_list()
    assert bar.value() == bar.maximum()
    assert view.tree.topLevelItem(39).text(5) == "Done"
    view.close()


def test_finished_batch_shows_a_summary_instead_of_the_last_progress(qapp, batch_manager):
    # 最後の項目の「自動カットの特徴の計算 82%」が残り、終わったのか分からなかった(2026-10-10)
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"))
    view.add_item(_file_info("b"))
    view.start()
    _wait_for_batch(batch_manager)
    qapp.processEvents()  # 本当の進捗を先に届けてから、最後の進捗を差し替える
    view._on_progress(ProcessProgress(current_file=2, total_files=2, current_phase="Features",
                                      progress_percent=82.0, elapsed_time=847.0, estimated_remaining=0.0))
    view._refresh_list()
    assert view.progress_label.text().startswith("終了  完了 2 件・エラー 0 件・スキップ 0 件")
    assert "経過 0:14:07" in view.progress_label.text()
    assert view.progress_bar.value() == 100


def test_output_name_column_and_rename_from_the_context_menu(qapp, batch_manager, monkeypatch):
    # 保存名称の列と、右クリックの「名称変更」(未処理・エラー・スキップの項目だけ、2026-10-11)
    from ts_smartcut.gui import output_name_dialog
    from ts_smartcut.gui.batch_list_view import COLUMN_OUTPUT

    out = batch_manager.settings.default_output_dir
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"), output_path=out / "第1話.mp4")
    view.add_item(_file_info("a"), output_path=out / "第2話.mp4")  # 同じ録画でも名前が違えば足せる
    assert view.tree.topLevelItem(1).text(COLUMN_OUTPUT) == "第2話.mp4"
    assert view.tree.topLevelItem(1).toolTip(COLUMN_OUTPUT) == str(out / "第2話.mp4")

    asked = []

    def answer(parent, initial, input_path, check, title="保存名称"):
        asked.append((initial, check(out / "第1話.mp4"), check(out / "前編.mp4")))
        return out / "前編.mp4"

    monkeypatch.setattr(output_name_dialog, "ask_output_name", answer)
    rename = view.item_menu(1).actions()[-1]
    assert rename.text() == "名称変更…" and rename.isEnabled()
    rename.trigger()
    # 初期値は今の名前。他の項目と同じ名前は誤り、新しい名前は正しい
    assert asked[0][0] == out / "第2話.mp4"
    assert "他の項目" in asked[0][1] and asked[0][2] is None
    assert batch_manager.items[1].output_path == out / "前編.mp4"
    assert view.tree.topLevelItem(1).text(COLUMN_OUTPUT) == "前編.mp4"

    batch_manager.items[1].status = "Done"
    assert not view.item_menu(1).actions()[-1].isEnabled()
    assert view.rename(1) is False


def test_output_name_dialog_returns_to_the_input_on_a_wrong_name(qapp, tmp_path, monkeypatch):
    # 誤りなら注意を出してダイアログに戻る(入れた名前はそのまま)。拡張子は元の録画と同じ
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ts_smartcut.gui import output_name_dialog
    from ts_smartcut.gui.output_name_dialog import OutputNameDialog

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    typed = iter(["既にある", "新しい名前"])
    texts_at_open = []

    def type_and_accept():
        dialog = next(w for w in QApplication.topLevelWidgets() if isinstance(w, OutputNameDialog) and w.isVisible())
        texts_at_open.append(dialog.name_edit.text())
        dialog.name_edit.setText(next(typed))
        dialog.accept()

    def check(path):
        return "同じ名前のファイルが既にあります" if path.stem == "既にある" else None

    timer = QTimer()
    timer.timeout.connect(type_and_accept)
    timer.start(50)
    try:
        result = output_name_dialog.REAL_ASK_OUTPUT_NAME(
            None, tmp_path / "録画_cut.ts", tmp_path / "録画.ts", check)
    finally:
        timer.stop()
    assert result == tmp_path / "新しい名前.ts"
    assert warnings == ["同じ名前のファイルが既にあります"]
    assert texts_at_open == ["録画_cut", "既にある"]


def test_duration_column_shows_the_kept_length_in_seconds():
    """再生時間の列: 残す区間の合計を秒まで(切り捨て)。2026-10-17 利用者の要望。"""
    from ts_smartcut.gui.batch_list_view import output_duration_text
    from pathlib import Path

    from ts_smartcut.models import CutRange, FileCutInfo

    info = FileCutInfo(file_path=Path("a.ts"), total_frames=200000, fps=30000 / 1001, resolution=(1440, 1080),
                       video_codec="mpeg2video", audio_codec="aac", bitrate=0, duration=0.0, file_size=0)
    info.cuts = [CutRange(0, 30000), CutRange(40000, 44612)]  # 34612 フレーム = 1154.9 秒
    assert output_duration_text(info) == "00:19:14"
    info.cuts = [CutRange(0, 120000)]  # 4004.0 秒
    assert output_duration_text(info) == "01:06:44"
    info.cuts = []
    assert output_duration_text(info) == "00:00:00"


def test_output_format_menu_changes_only_that_item(qapp, batch_manager, tmp_path):
    # 出力形式(設計書 §6.9)は項目ごと。AVS／VPY の項目は処理モードを使わず、モード欄は形式を出す
    view = BatchListView(batch_manager)
    view.add_item(_file_info("a"), output_path=tmp_path / "a_cut.ts")
    view.add_item(_file_info("b"), output_path=tmp_path / "b_cut.ts")
    menu = view.item_menu(0)
    format_menu = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == tr("batch.output_format_menu"))
    assert [a.text() for a in format_menu.actions()] == ["TS", "AVS (AviSynth)", "VPY (VapourSynth)"]
    format_menu.actions()[2].trigger()
    assert batch_manager.items[0].output_format == "vpy"
    assert batch_manager.items[0].output_path.name == "a_cut.vpy"
    assert batch_manager.items[1].output_format == "ts"
    assert view.tree.topLevelItem(0).text(4) == "VPY (VapourSynth)"
    assert not any(a.isEnabled() for a in view.mode_menu(0).actions())
    batch_manager.items[0].status = "Done"
    menu = view.item_menu(0)
    format_menu = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == tr("batch.output_format_menu"))
    assert not any(a.isEnabled() for a in format_menu.actions())
