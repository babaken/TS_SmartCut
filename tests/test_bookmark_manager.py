from ts_smartcut.core.bookmark_manager import BookmarkManager


def test_add_bookmark_default_name():
    mgr = BookmarkManager()
    bm = mgr.add_bookmark(100)
    assert bm.frame == 100
    assert bm.name == "Bookmark 1"
    assert mgr.bookmarks == [bm]


def test_add_bookmark_custom_name():
    mgr = BookmarkManager()
    bm = mgr.add_bookmark(100, "CM開始")
    assert bm.name == "CM開始"


def test_bookmarks_kept_sorted_by_frame():
    mgr = BookmarkManager()
    mgr.add_bookmark(500)
    mgr.add_bookmark(100)
    mgr.add_bookmark(300)
    assert [b.frame for b in mgr.bookmarks] == [100, 300, 500]


def test_remove_bookmark():
    mgr = BookmarkManager()
    mgr.add_bookmark(100)
    mgr.add_bookmark(200)
    assert mgr.remove_bookmark(0) is True
    assert [b.frame for b in mgr.bookmarks] == [200]


def test_remove_bookmark_invalid_index():
    mgr = BookmarkManager()
    assert mgr.remove_bookmark(0) is False


def test_get_bookmark():
    mgr = BookmarkManager()
    bm = mgr.add_bookmark(100)
    assert mgr.get_bookmark(0) == bm
    assert mgr.get_bookmark(5) is None


def test_clear():
    mgr = BookmarkManager()
    mgr.add_bookmark(100)
    mgr.add_bookmark(200)
    mgr.clear()
    assert mgr.bookmarks == []
