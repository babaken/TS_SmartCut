from pathlib import Path

import pytest

from ts_smartcut.core.cut_manager import MAX_HISTORY, CutManager
from ts_smartcut.errors import ValidationError
from ts_smartcut.models import FileCutInfo


@pytest.fixture
def file_info() -> FileCutInfo:
    return FileCutInfo(
        file_path=Path("/videos/sample.mp4"),
        total_frames=1000,
        fps=30.0,
        resolution=(1920, 1080),
        video_codec="h264",
        audio_codec="aac",
        bitrate=5_000_000,
        duration=33.3,
        file_size=123456,
    )


@pytest.fixture
def manager(file_info) -> CutManager:
    return CutManager(file_info)


# ---- add_cut ----------------------------------------------------------

def test_add_cut_valid(manager):
    assert manager.add_cut(100, 200, "CM") is True
    assert len(manager.cuts) == 1
    assert manager.cuts[0].start_frame == 100
    assert manager.cuts[0].label == "CM"


def test_add_cut_keeps_sorted_order(manager):
    manager.add_cut(500, 600)
    manager.add_cut(100, 200)
    assert [c.start_frame for c in manager.cuts] == [100, 500]


def test_add_cut_overlap_raises(manager):
    manager.add_cut(100, 200)
    with pytest.raises(ValidationError) as exc_info:
        manager.add_cut(150, 250)
    assert exc_info.value.code == "E202"
    assert len(manager.cuts) == 1


def test_add_cut_adjacent_ranges_do_not_overlap(manager):
    manager.add_cut(100, 200)
    assert manager.add_cut(200, 300) is True


def test_add_cut_out_of_range_raises_e203(manager):
    with pytest.raises(ValidationError) as exc_info:
        manager.add_cut(-10, 50)
    assert exc_info.value.code == "E203"

    with pytest.raises(ValidationError) as exc_info:
        manager.add_cut(900, 1100)
    assert exc_info.value.code == "E203"


def test_add_cut_start_after_end_raises_e204(manager):
    with pytest.raises(ValidationError) as exc_info:
        manager.add_cut(500, 500)
    assert exc_info.value.code == "E204"

    with pytest.raises(ValidationError) as exc_info:
        manager.add_cut(500, 400)
    assert exc_info.value.code == "E204"


# ---- remove_cut ---------------------------------------------------------

def test_remove_cut_valid_index(manager):
    manager.add_cut(100, 200)
    assert manager.remove_cut(0) is True
    assert manager.cuts == []


def test_remove_cut_invalid_index_returns_false(manager):
    assert manager.remove_cut(0) is False


# ---- edit_cut -----------------------------------------------------------

def test_edit_cut_valid(manager):
    manager.add_cut(100, 200)
    assert manager.edit_cut(0, 150, 250) is True
    assert manager.cuts[0].start_frame == 150
    assert manager.cuts[0].end_frame == 250


def test_edit_cut_preserves_label(manager):
    manager.add_cut(100, 200, "CM")
    manager.edit_cut(0, 150, 250)
    assert manager.cuts[0].label == "CM"


def test_edit_cut_overlap_with_other_raises(manager):
    manager.add_cut(100, 200)
    manager.add_cut(300, 400)
    with pytest.raises(ValidationError) as exc_info:
        manager.edit_cut(0, 150, 350)
    assert exc_info.value.code == "E202"


def test_edit_cut_invalid_index_returns_false(manager):
    assert manager.edit_cut(0, 100, 200) is False


# ---- undo/redo ------------------------------------------------------------

def test_undo_redo_add_cut(manager):
    manager.add_cut(100, 200)
    assert manager.can_undo is True
    assert manager.can_redo is False

    assert manager.undo() is True
    assert manager.cuts == []
    assert manager.can_redo is True

    assert manager.redo() is True
    assert len(manager.cuts) == 1
    assert manager.cuts[0].start_frame == 100


def test_undo_redo_remove_cut(manager):
    manager.add_cut(100, 200)
    manager.remove_cut(0)
    assert manager.cuts == []

    manager.undo()
    assert len(manager.cuts) == 1
    assert manager.cuts[0].start_frame == 100

    manager.redo()
    assert manager.cuts == []


def test_undo_redo_edit_cut(manager):
    manager.add_cut(100, 200)
    manager.edit_cut(0, 150, 250)
    assert manager.cuts[0].start_frame == 150

    manager.undo()
    assert manager.cuts[0].start_frame == 100

    manager.redo()
    assert manager.cuts[0].start_frame == 150


def test_undo_with_empty_history_returns_false(manager):
    assert manager.undo() is False


def test_redo_with_nothing_to_redo_returns_false(manager):
    manager.add_cut(100, 200)
    assert manager.redo() is False


def test_new_operation_after_undo_clears_redo_stack(manager):
    manager.add_cut(100, 200)
    manager.add_cut(300, 400)
    manager.undo()  # 300-400 の追加を取り消す -> cuts = [100-200]

    manager.add_cut(500, 600)  # 新しい操作 -> redoスタックは破棄される

    assert manager.can_redo is False
    assert [c.start_frame for c in manager.cuts] == [100, 500]


def test_undo_redo_multiple_cuts_out_of_order(manager):
    manager.add_cut(500, 600)
    manager.add_cut(100, 200)  # ソートによりインデックスが入れ替わる

    manager.undo()  # 100-200 の追加を取り消す（値ベースで正しく戻る）
    assert [c.start_frame for c in manager.cuts] == [500]

    manager.redo()
    assert [c.start_frame for c in manager.cuts] == [100, 500]


def test_history_capped_at_max_entries(manager):
    for i in range(MAX_HISTORY + 5):
        manager.add_cut(i * 10, i * 10 + 5)

    assert len(manager.history) == MAX_HISTORY
    assert len(manager.cuts) == MAX_HISTORY + 5

    # 履歴が上限を超えた分は古い操作から失われるため、
    # 全件Undoしてもcutsが0件にはならない。
    undo_count = 0
    while manager.undo():
        undo_count += 1
    assert undo_count == MAX_HISTORY
    assert len(manager.cuts) == 5


# ---- すべてクリア (design doc §4.2) -----------------------------------------------

def test_clear_all_removes_cuts_and_bookmarks_and_undoes_in_one_step(file_info):
    from ts_smartcut.core.bookmark_manager import BookmarkManager

    manager, bookmarks = CutManager(file_info), BookmarkManager()
    manager.add_cut(10, 20)
    manager.add_cut(50, 60)
    bookmarks.add_bookmark(30, "CM")
    saved_cuts, saved_bookmarks = list(manager.cuts), list(bookmarks.bookmarks)

    assert manager.clear_all(bookmarks) is True
    assert manager.cuts == [] and bookmarks.bookmarks == []

    late = bookmarks.add_bookmark(5, "クリア後")  # ブックマークの追加は履歴に残らない
    assert manager.undo() is True
    assert manager.cuts == saved_cuts
    assert bookmarks.bookmarks == sorted(saved_bookmarks + [late], key=lambda b: b.frame)

    assert manager.redo() is True
    assert manager.cuts == [] and bookmarks.bookmarks == [late]


def test_clear_all_with_nothing_to_clear_records_no_history(file_info):
    manager = CutManager(file_info)
    assert manager.clear_all() is False
    assert manager.can_undo is False


def test_clear_all_without_bookmark_manager_clears_cuts(file_info):
    manager = CutManager(file_info)
    manager.add_cut(10, 20)
    assert manager.clear_all() is True and manager.cuts == []
    manager.undo()
    assert [(c.start_frame, c.end_frame) for c in manager.cuts] == [(10, 20)]


# ---- カット区間の合計 (design doc §4.8) ----------------------------------------------

def test_total_cut_frames_sums_ranges_excluding_end_frame(file_info):
    manager = CutManager(file_info)
    assert manager.total_cut_frames == 0
    manager.add_cut(10, 20)    # 10フレーム(20は含まない)
    manager.add_cut(100, 130)  # 30フレーム
    assert manager.total_cut_frames == 40
    manager.edit_cut(0, 10, 25)
    assert manager.total_cut_frames == 45
    manager.undo()
    assert manager.total_cut_frames == 40
    manager.remove_cut(1)
    assert manager.total_cut_frames == 10


def test_add_cut_keeps_excluded_mark_on_the_added_range(manager):
    # プロジェクトの復元: 開始順でない区間を足しても、印は足した区間そのものに付く
    manager.add_cut(500, 600)
    manager.add_cut(100, 200, is_excluded=True)

    assert [(c.start_frame, c.is_excluded) for c in manager.cuts] == [(100, True), (500, False)]


def test_toggle_excluded_is_undoable_and_redoable(manager):
    manager.add_cut(100, 200)

    assert manager.toggle_excluded(0) is True
    assert manager.cuts[0].is_excluded is True
    assert manager.undo() is True
    assert manager.cuts[0].is_excluded is False
    assert manager.redo() is True
    assert manager.cuts[0].is_excluded is True
    assert [event["event_type"] for event in manager.edit_events] == [
        "add_cut", "toggle_excluded", "undo", "redo"
    ]
