import os
import sys
import time

import pytest

from ts_smartcut.system.temp_file_manager import TempFileManager, locked_run_dir, remove_stale_temp_files


def test_create_temp_file_reserves_path_without_creating_it(tmp_path):
    mgr = TempFileManager(tmp_path / "ts_smartcut")
    path = mgr.create_temp_file(suffix=".mp4")
    assert path.parent == mgr.temp_dir
    assert path.name.startswith("temp_")
    assert path.suffix == ".mp4"
    assert not path.exists()
    assert path in mgr.active_files


def test_cleanup_deletes_created_files(tmp_path):
    mgr = TempFileManager(tmp_path / "ts_smartcut")
    path = mgr.create_temp_file()
    path.write_bytes(b"dummy")
    assert path.exists()

    mgr.cleanup()

    assert not path.exists()
    assert mgr.active_files == []


def test_cleanup_ignores_files_never_written(tmp_path):
    mgr = TempFileManager(tmp_path / "ts_smartcut")
    mgr.create_temp_file()  # never written to disk
    mgr.cleanup()  # should not raise
    assert mgr.active_files == []


# ---- 処理ごとの一時フォルダと前回の残りの削除 (design doc §11.2) ----------------

def test_locked_run_dir_is_removed_on_exit(tmp_path):
    with locked_run_dir(tmp_path) as run_dir:
        assert run_dir.parent == tmp_path and run_dir.name.startswith("run_")
        (run_dir / "temp_part.ts").write_bytes(b"x")
    assert not run_dir.exists()


def test_stale_run_dir_is_removed_and_other_files_are_kept(tmp_path):
    # 異常終了で残ったフォルダ(ロックは外れている)だけを消す。直下のファイルには触れない
    # (処理ごとのフォルダを使う前の版が直下に残した temp_* を消す処理は 2026-11-19 に外した)
    stale = tmp_path / "run_old"
    stale.mkdir()
    (stale / ".lock").write_bytes(b"")
    (stale / "temp_a.ts").write_bytes(b"x" * 100)
    (tmp_path / "temp_b.ts").write_bytes(b"x" * 50)
    (tmp_path / "keep.txt").write_bytes(b"x")  # このアプリの一時ファイルではない

    summary = remove_stale_temp_files(tmp_path)

    assert (summary.count, summary.total_bytes) == (1, 100)
    assert not stale.exists()
    assert (tmp_path / "temp_b.ts").exists() and (tmp_path / "keep.txt").exists()


def test_run_dir_in_use_is_kept(tmp_path):
    # 処理中(ロック中)のフォルダは、経過時間に関係なく消さない
    with locked_run_dir(tmp_path) as run_dir:
        part = run_dir / "temp_part.ts"
        part.write_bytes(b"x")
        old = time.time() - 7 * 86400
        os.utime(part, (old, old))
        assert remove_stale_temp_files(tmp_path).count == 0
        assert part.exists()


def test_remove_stale_temp_files_without_folder(tmp_path):
    assert remove_stale_temp_files(tmp_path / "missing").count == 0


@pytest.mark.skipif(sys.platform != "win32", reason="開いているファイルを消せないのはWindowsだけ")
def test_files_still_open_are_kept_and_not_counted(tmp_path):
    # 異常終了したアプリのffmpegがまだ書いているファイルは消せない。消せた分だけ数える
    stale = tmp_path / "run_old"
    stale.mkdir()
    (stale / ".lock").write_bytes(b"")
    (stale / "temp_done.ts").write_bytes(b"x" * 100)
    with open(stale / "temp_writing.ts", "wb") as writing:
        writing.write(b"x" * 30)
        writing.flush()
        summary = remove_stale_temp_files(tmp_path)
    assert (summary.count, summary.total_bytes) == (1, 100)
    assert (stale / "temp_writing.ts").exists()
    assert remove_stale_temp_files(tmp_path).count == 1  # 閉じた後なら消せる
    assert not stale.exists()
