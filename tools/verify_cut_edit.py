"""選んだ区間を S / E で直す・Home / End で跳ぶ・Esc で選択を外す(設計書 §4.3、2026-10-14)を、本物の画面で確かめる。

使い方(プロジェクトのルートで、画面のあるデスクトップ上で):
    python tools/verify_cut_edit.py

本物のアプリの画面(MainWindow)を開き、ffmpeg で作るテスト映像(放送の録画は使わない)に、キーを押した時と
同じ入力(QTest のキーの押下)を送って確かめる。最後に直した区間で「エンコード開始」(Smart Cut)を行い、
出力のフレーム数を数える。設定・記録は一時フォルダを使い、利用者の設定は変えない。結果は項目ごとに
合格 / 不合格 で出し、すべて合格なら終了コード 0。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="ts_smartcut_verify_cut_edit_"))
os.environ["TS_SMARTCUT_CONFIG_DIR"] = str(WORK / "config" / "ts_smartcut")  # settings_manager.CONFIG_DIR_ENV
os.environ["XDG_CONFIG_HOME"] = str(WORK / "config")
os.environ["APPDATA"] = str(WORK / "config")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

SECONDS = 40
FPS = "30000/1001"
RESULTS = []


def make_test_video(path: Path) -> Path:
    """MPEG-2 1080i 相当の合成映像(GOP 15、B フレームあり)と音声の TS。"""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s=1440x1080:r={FPS}:d={SECONDS}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={SECONDS}",
         "-c:v", "mpeg2video", "-b:v", "8M", "-g", "15", "-bf", "2", "-flags", "+ilme+ildct",
         "-c:a", "aac", "-b:a", "192k", str(path)],
        check=True,
    )
    return path


def wait(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def wait_until(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        wait(0.03)
    return condition()


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'合格' if ok else '不合格'}] {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    print(f"テスト映像を作っています… ({WORK})")
    video = make_test_video(WORK / "テスト映像.ts")

    app = QApplication(sys.argv)
    from ts_smartcut.gui.main_window import MainWindow
    from ts_smartcut.models import AppSettings, CutProposal

    # 本物の入力画面・知らせの画面は出さない(出すと押されるまで止まる)。出たことだけ記録する
    dialogs = []
    QInputDialog.getInt = staticmethod(lambda *a, **k: (dialogs.append(("getInt", a[2] if len(a) > 2 else "")), (0, False))[1])
    QMessageBox.information = staticmethod(lambda *a, **k: dialogs.append(("information", a[2] if len(a) > 2 else "")))
    QMessageBox.warning = staticmethod(lambda *a, **k: dialogs.append(("warning", a[2] if len(a) > 2 else "")))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

    MainWindow.autocut_proposals = False
    settings = AppSettings(default_output_dir=WORK / "out")
    (WORK / "out").mkdir()
    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()
    window.activateWindow()
    window.raise_()
    wait(1.5)
    window.open_file(video)
    if not wait_until(lambda: window.preview_widget.get_current_frame() is not None, 15):
        print("mpv につながらないため確かめられません(プレビューが要ります)")
        return 2
    window.preview_widget.controller._mpv.pause = True

    def frame():
        return window.preview_widget.get_current_frame()

    def settle(target, timeout=5):
        """target へ動き、絵の表示まで落ち着くのを待つ(mpv は移動の指示の直後に位置だけ先に変わり、
        すぐに ← を送ると効かないことがあった)。"""
        mpv = window.preview_widget.controller._mpv
        wait_until(lambda: frame() == target and not getattr(mpv, "seeking", False), timeout)
        wait(0.3)

    def key(k, settle_frame=None):
        """キーを押す(QShortcut を通る本物の入力)。settle_frame まで位置が動くのを待つ。"""
        window.activateWindow()
        QTest.keyClick(window, k)
        QApplication.processEvents()
        if settle_frame is not None:
            settle(settle_frame)
        else:
            wait(0.15)

    def seek(target):
        window.preview_widget.seek_frame(target)
        settle(target)

    def ranges():
        return [(c.start_frame, c.end_frame) for c in window.cut_manager.cuts]

    def select_row(row):
        item = window.cut_list.topLevelItem(row)
        window.cut_list.setCurrentItem(item)
        window.cut_list.itemClicked.emit(item, 0)  # 行のクリック(開始へ移動)
        QApplication.processEvents()

    # ---- 1. 100-1001 → 99-1000 を 6 操作で --------------------------------------------
    window.cut_manager.add_cut(100, 1001)
    window._refresh_cut_list()
    select_row(0)
    settle(100)
    check("1-1 行のクリックで開始 100 へ移動", frame() == 100, f"フレーム {frame()}")
    key(Qt.Key.Key_Left, 99)
    check("1-2 ← で 99 へ", frame() == 99, f"フレーム {frame()}")
    key(Qt.Key.Key_S)
    check("1-3 S で開始が 99 に", ranges() == [(99, 1001)], str(ranges()))
    check("1-3 選んだまま・開始へ移動しない", window._selected_cut_index == 0 and frame() == 99,
          f"選択 {window._selected_cut_index}, フレーム {frame()}")
    key(Qt.Key.Key_End, 1001)
    check("1-4 End で終了 1001 へ移動", frame() == 1001, f"フレーム {frame()}")
    key(Qt.Key.Key_Left, 1000)
    check("1-5 ← で 1000 へ", frame() == 1000, f"フレーム {frame()}")
    key(Qt.Key.Key_E)
    check("1-6 E で 99-1000 に", ranges() == [(99, 1000)], str(ranges()))
    check("1-7 入力画面・知らせの画面は開かない", dialogs == [], str(dialogs))
    check("1-8 状態の表示が選んだ区間", "99-1000" in window.pending_label.text(), window.pending_label.text())

    # ---- 2. Ctrl+Z / Ctrl+Y ------------------------------------------------------------
    for _ in range(2):
        QTest.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
    check("2-1 Ctrl+Z 2 回で 100-1001 に戻る", ranges() == [(100, 1001)], str(ranges()))
    for _ in range(2):
        QTest.keyClick(window, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
    check("2-2 Ctrl+Y 2 回で 99-1000 に戻る", ranges() == [(99, 1000)], str(ranges()))

    # ---- 3. Home / End --------------------------------------------------------------
    select_row(0)
    settle(99)
    key(Qt.Key.Key_End, 1000)
    check("3-1 End で選んだ区間の終了へ", frame() == 1000, f"フレーム {frame()}")
    key(Qt.Key.Key_Home, 99)
    check("3-2 Home で選んだ区間の開始へ", frame() == 99, f"フレーム {frame()}")

    # ---- 4. 未判断の案の行 ------------------------------------------------------------
    window._on_proposals_ready(window._program_info_request, [CutProposal(1100, 1150, 0.9, "案")], "")
    select_row(1)
    settle(1100)
    key(Qt.Key.Key_Left, 1099)
    key(Qt.Key.Key_S)
    pending = window.cut_manager.pending_proposals
    check("4-1 案の行で S → 案も区間も変わらない",
          ranges() == [(99, 1000)] and pending and pending[0].start_frame == 1100, str(ranges()))
    check("4-2 「先に承認してください」が出る", "承認" in window.status_bar.currentMessage(),
          window.status_bar.currentMessage())
    window.accept_proposal_button.click()
    QApplication.processEvents()
    select_row(1)
    settle(1100)
    key(Qt.Key.Key_Left, 1099)
    before = frame()
    key(Qt.Key.Key_S)
    check("4-3 承認した後は S で直せる", ranges() == [(99, 1000), (1099, 1150)], f"{ranges()} / 押す前のフレーム {before}")

    # ---- 5. 書き換えられない時 --------------------------------------------------------
    dialogs.clear()
    select_row(1)
    settle(1099)
    seek(990)
    key(Qt.Key.Key_S)  # 990-1150 は 99-1000 と重なる
    check("5-1 重なる S → 変わらずエラー", ranges() == [(99, 1000), (1099, 1150)]
          and any("E202" in str(d[1]) for d in dialogs), str(dialogs))
    dialogs.clear()
    seek(1160)
    key(Qt.Key.Key_S)  # 開始 1160 > 終了 1150
    check("5-2 開始 ≥ 終了になる S → 変わらずエラー", ranges() == [(99, 1000), (1099, 1150)]
          and any("E204" in str(d[1]) for d in dialogs), str(dialogs))

    # ---- 6. Esc の後は新しい区間用 ------------------------------------------------------
    dialogs.clear()
    key(Qt.Key.Key_Escape)
    check("6-1 Esc で選択が外れる", window._selected_cut_index is None, str(window._selected_cut_index))
    seek(1170)
    key(Qt.Key.Key_S)
    seek(1180)
    key(Qt.Key.Key_E)
    check("6-2 選んでいない S / E は新しい区間用に覚える",
          ranges() == [(99, 1000), (1099, 1150)] and (window._pending_start, window._pending_end) == (1170, 1180),
          f"{ranges()} / {window._pending_start}-{window._pending_end}")
    check("6-3 Home / End は選んでいないと押せない",
          not window.jump_start_button.isEnabled() and not window.jump_end_button.isEnabled())

    # ---- 7. 直した区間でエンコード -----------------------------------------------------
    window.cut_manager.remove_cut(1)
    window._refresh_cut_list()
    window._pending_start = window._pending_end = None
    window._update_pending_label()
    window._encode_hardware = None
    window._on_start_encode()
    finished = wait_until(lambda: not window.is_encoding(), 600)
    item = window.encode_manager.items[0]
    output = item.output_path
    count = None
    if finished and output is not None and Path(output).is_file():
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        numbers = [int(x) for x in probe.stdout.split() if x.isdigit()]
        count = numbers[0] if numbers else None
    check("7-1 99-1000 でエンコードし、出力が 901 フレーム", count == 901,
          f"状態 {item.status}, 出力 {output}, フレーム {count}")

    window.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} / {len(RESULTS)} 合格")
    return 0 if not failed else 1


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
