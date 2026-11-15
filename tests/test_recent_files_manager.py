import json

from ts_smartcut.core.recent_files_manager import RecentFilesManager


def _touch(path):
    path.write_bytes(b"")
    return path


def test_add_moves_to_front_without_duplicates_and_persists(tmp_path):
    store = tmp_path / "recent_files.json"
    a, b = _touch(tmp_path / "a.ts"), _touch(tmp_path / "b.ts")
    mgr = RecentFilesManager(store)

    mgr.add(a)
    mgr.add(b)
    mgr.add(a)

    assert mgr.files == [a, b]
    assert json.loads(store.read_text(encoding="utf-8")) == [str(a), str(b)]
    assert RecentFilesManager(store).files == [a, b]


def test_add_trims_to_max_count(tmp_path):
    mgr = RecentFilesManager(tmp_path / "recent_files.json", max_count=2)
    for name in ("a.ts", "b.ts", "c.ts"):
        mgr.add(tmp_path / name)

    assert mgr.files == [tmp_path / "c.ts", tmp_path / "b.ts"]


def test_set_max_count_shrinks_list(tmp_path):
    mgr = RecentFilesManager(tmp_path / "recent_files.json")
    for name in ("a.ts", "b.ts", "c.ts"):
        mgr.add(tmp_path / name)

    mgr.set_max_count(1)

    assert mgr.files == [tmp_path / "c.ts"]


def test_remove_missing_drops_nonexistent_files(tmp_path):
    store = tmp_path / "recent_files.json"
    kept = _touch(tmp_path / "kept.ts")
    mgr = RecentFilesManager(store)
    mgr.add(tmp_path / "gone.ts")
    mgr.add(kept)

    mgr.remove_missing()

    assert mgr.files == [kept]
    assert RecentFilesManager(store).files == [kept]


def test_remove_and_clear(tmp_path):
    mgr = RecentFilesManager(tmp_path / "recent_files.json")
    mgr.add(tmp_path / "a.ts")
    mgr.add(tmp_path / "b.ts")

    mgr.remove(tmp_path / "a.ts")
    assert mgr.files == [tmp_path / "b.ts"]

    mgr.clear()
    assert mgr.files == []


def test_corrupt_store_falls_back_to_empty(tmp_path):
    store = tmp_path / "recent_files.json"
    store.write_text("{not json", encoding="utf-8")

    assert RecentFilesManager(store).files == []


def test_unwritable_store_does_not_raise(tmp_path):
    blocker = _touch(tmp_path / "not_a_dir")
    mgr = RecentFilesManager(blocker / "recent_files.json")

    mgr.add(tmp_path / "a.ts")

    assert mgr.files == [tmp_path / "a.ts"]
