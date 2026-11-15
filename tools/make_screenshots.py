"""設計書・README用の画面のスクリーンショットを作る(Windows/Linux共通)。

使い方(プロジェクトのルートで、画面のあるデスクトップ上で):
    python tools/make_screenshots.py            # → docs/images/*.png
    python tools/make_screenshots.py --language en --out <フォルダ>   # 英語の画面の確認用(資料には入れない)

実アプリ(MainWindowと各ダイアログ)を起動して画像に保存する。プレビューに映すのは
ffmpegで作るテスト映像で、放送の画像は使わない(リポジトリに入れないため)。
設定・履歴・ハードウェア判定は一時フォルダを使い、利用者の設定は変えない。
画面に出るパスのユーザー名(一時フォルダはユーザーのフォルダの下にある)は、撮る前に伏せ字にする
(hide_user_name。保存名称のダイアログの「保存先」・設定の自動カットの「記録 DB」に出ていた、2026-10-14・15)。
画面を変えたら撮り直す。
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
WORK = Path(tempfile.mkdtemp(prefix="ts_smartcut_shots_"))
# 利用者の設定・最近使用したファイル・ハードウェア判定の結果を変えない
os.environ["TS_SMARTCUT_CONFIG_DIR"] = str(WORK / "config" / "ts_smartcut")  # settings_manager.CONFIG_DIR_ENV
os.environ["XDG_CONFIG_HOME"] = str(WORK / "config")  # mpv など外部の設定
os.environ["APPDATA"] = str(WORK / "config")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QPoint, QRect, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

SECONDS = 120
FPS = "30000/1001"
CM_START, CM_END = 40.0, 70.0  # テスト映像の「CM」の位置(秒)


def make_test_video(path: Path) -> Path:
    """カラーバー等の合成映像(MPEG-2 1080i相当)と、CMの境目に無音のある音声のTS。"""
    gaps = "*".join(f"not(between(t,{t - 0.4:.1f},{t + 0.1:.1f}))" for t in (CM_START, CM_END))
    loud = f"(0.35+0.25*between(t,{CM_START},{CM_END}))"  # CMは音が大きい
    audio = f"aevalsrc='{loud}*sin(2*PI*440*t)*(0.55+0.45*sin(2*PI*0.8*t))*{gaps}':s=48000:d={SECONDS}"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s=1440x1080:r={FPS}:d={SECONDS}",
         "-f", "lavfi", "-i", audio,
         "-c:v", "mpeg2video", "-b:v", "8M", "-g", "15", "-bf", "2", "-flags", "+ilme+ildct",
         "-c:a", "aac", "-b:a", "192k", str(path)],
        check=True,
    )
    return path


def wait(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def save(image, name: str) -> None:
    path = OUT / name
    image.save(str(path))
    print(f"  {path}")


USER_MASK = "＊＊＊"


def hide_user_name(widget: QWidget) -> None:
    """画面の文字(ラベル・入力欄)に出るユーザー名を伏せ字にする。"""
    from PySide6.QtWidgets import QLabel, QLineEdit

    names = {getpass.getuser(), Path.home().name} - {""}
    for child in [widget] + widget.findChildren(QWidget):
        if isinstance(child, (QLabel, QLineEdit)):
            text = child.text()
            for name in names:
                for sep in ("\\", "/"):
                    text = text.replace(f"{sep}{name}{sep}", f"{sep}{USER_MASK}{sep}")
            if text != child.text():
                child.setText(text)


def grab_shown(widget: QWidget, name: str) -> None:
    """ダイアログ等を表示して(レイアウトを確定させて)から撮る。"""
    widget.show()
    hide_user_name(widget)
    wait(0.4)
    save(widget.grab(), name)
    widget.close()


def side_by_side(images, gap: int = 12) -> QImage:
    # 表示倍率が 100% でない時、撮った画像は devicePixelRatio を持ち、そのまま描くと
    # 論理の大きさ(1/倍率)に縮めて描かれて文字が崩れた。画素のまま並べる
    ratio = images[0].devicePixelRatio()
    gap = round(gap * ratio)
    images = [QImage(i) for i in images]
    for image in images:
        image.setDevicePixelRatio(1.0)
    width = sum(i.width() for i in images) + gap * (len(images) - 1)
    height = max(i.height() for i in images)
    result = QImage(width, height, QImage.Format.Format_ARGB32)
    result.fill(QColor(0, 0, 0, 0))
    painter = QPainter(result)
    x = 0
    for image in images:
        painter.drawImage(x, 0, image)
        x += image.width() + gap
    painter.end()
    return result


def main() -> int:
    import argparse

    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="jp", help="画面の言語(jp / en)。資料の画像は jp")
    parser.add_argument("--out", type=Path, default=OUT, help="保存先(既定は docs/images)")
    args = parser.parse_args()
    OUT = args.out.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    print("テスト映像を作っています…")
    video1 = make_test_video(WORK / "サンプル番組_01.ts")
    video2 = WORK / "サンプル番組_02.ts"
    video2.write_bytes(video1.read_bytes())

    app = QApplication(sys.argv)
    from ts_smartcut.ffmpeg.smart_cut import MODE_SIMPLE_COPY
    from ts_smartcut.gui import output_name_dialog
    from ts_smartcut.gui.main_window import MainWindow
    from ts_smartcut.gui.settings_dialog import SettingsDialog
    from ts_smartcut.gui.shortcuts_dialog import ShortcutsDialog
    from ts_smartcut.gui.system_info_dialog import SystemInfoDialog
    from ts_smartcut.i18n.language_manager import tr
    from ts_smartcut.models import AppSettings

    settings = AppSettings(language=args.language)
    # 自動カットの案は録画を読んで作る(テスト映像でも出て、バッチ追加の確認で止まる)ので
    # 止め、案の画面は決まった案を入れて撮る
    MainWindow.autocut_proposals = False
    # バッチ追加の保存名称のダイアログ(§8)は初期値のまま OK したことにする(ダイアログは別に撮る)
    output_name_dialog.ask_output_name = lambda parent, initial, *args, **kwargs: initial
    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()
    wait(1.5)

    # バッチリスト用に2本追加しておく(2本目は Simple Copy)
    window.open_file(video2)
    window.cut_manager.add_cut(1199, 2098)
    window._on_add_to_batch()

    print("画面を撮っています…")
    window.open_file(video1)
    wait(2.5)  # mpvの読み込み(読み込み前のシークは失敗する)
    fps = window.file_info.fps
    # 残す区間(本編)。番組の前とCMが消える
    window.cut_manager.add_cut(round(5 * fps), round(CM_START * fps))
    window.cut_manager.add_cut(round(CM_END * fps), window.file_info.total_frames)
    window.bookmark_manager.add_bookmark(round(25 * fps), "CM前の場面転換")
    window._refresh_cut_list()
    window._refresh_bookmark_list()
    window.preview_widget.seek_frame(round((CM_START - 3) * fps))
    wait(4)  # 波形の計算とシーク
    window._poll_position()
    wait(0.5)
    save(window.grab(), "main_window.png")

    # §4.8 残す区間の見出しと一覧
    title, cut_list = window.cut_list_title, window.cut_list
    top_left = title.mapTo(window, QPoint(0, 0))
    bottom_right = cut_list.mapTo(window, QPoint(cut_list.width(), min(cut_list.height(), 110)))
    save(window.grab(QRect(top_left, bottom_right)), "cut_list_total.png")

    # §4.2 メニュー(ファイル/編集/表示/ツール/ヘルプ)
    menus = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is None:
            continue
        menu.popup(window.mapToGlobal(QPoint(0, 0)))
        wait(0.3)
        menus.append(menu.grab().toImage())
        menu.hide()
    save(side_by_side(menus), "menus.png")

    # §23 設定ダイアログ(タブごと)
    dialog = SettingsDialog(settings, window, window.preview_widget.get_audio_devices(),
                            history_db=window.history_db, shortcut_presets=window.shortcut_presets)
    tabs = dialog._tabs
    dialog.show()
    hide_user_name(dialog)  # 自動カットのタブの「記録 DB」の場所 (2026-10-15)
    wait(0.5)
    for index, name in enumerate(["general", "preview", "encode", "ui", "shortcuts", "advanced", "autocut"]):
        tabs.setCurrentIndex(index)
        wait(0.3)
        save(dialog.grab(), f"settings_{name}.png")
    # 自動カットのタブの中のタブ(全般は上の settings_autocut.png)
    for index, name in ((1, "autocut_stations"), (2, "autocut_thresholds"), (3, "autocut_learning")):
        dialog.autocut_panel.tabs.setCurrentIndex(index)
        wait(0.3)
        save(dialog.grab(), f"settings_{name}.png")
    dialog.reject()

    # §4.12 音声の選択(試験用の動画は音声が 1 つなので、音声の一覧は試しの 2 つ)
    from ts_smartcut.broadcast.audio_tracks import AudioTrack
    from ts_smartcut.gui.audio_select_dialog import AudioSelectDialog
    from ts_smartcut.gui.main_window import _AudioDialogPreview

    described = "解説" if args.language == "jp" else "Audio description"  # 放送局が入れる説明の例
    tracks = [AudioTrack(0, 0x112, 0x10, 0x03, "jpn"), AudioTrack(1, 0x113, 0x11, 0x03, "jpn", text=described)]
    audio_dialog = AudioSelectDialog(tracks, window.file_info.total_frames, fps, round(10 * fps),
                                     _AudioDialogPreview(window), window)
    grab_shown(audio_dialog, "audio_select.png")
    window.preview_widget.command(["set", "pause", "yes"])

    # §22 システム情報、§25.1 ショートカット一覧
    grab_shown(SystemInfoDialog(settings, window), "system_info.png")
    shortcuts = ShortcutsDialog(window, window.shortcut_presets.active, window._preset_name(0))
    # 一覧がすべて写る高さ。操作が 24 個になり(2026-11-15)、表示すると画面の高さに縮められて切れたので、
    # 表示せずに撮る(表示していない窓は縮められない)
    shortcuts.resize(420, 820)
    shortcuts.ensurePolished()
    shortcuts.layout().activate()
    save(shortcuts.grab(), "shortcuts.png")

    # §16.3 VFRの警告(VFRのファイルを開いた時と同じ文面)
    grab_shown(QMessageBox(QMessageBox.Icon.Warning, tr("main.vfr_title"), tr("main.vfr_warning"), parent=window),
               "vfr_warning.png")

    # バッチリストと、項目の右クリックメニュー(処理モード・出力形式・名称変更)
    window._on_add_to_batch()
    window.batch_manager.set_processing_mode(0, MODE_SIMPLE_COPY)
    window.batch_list_view._refresh_list()
    # バッチリストはタブ(2026-10-11)。タブごと撮る
    window._on_show_batch_tab()
    wait(0.5)
    save(window.lists_tabs.grab(), "batch_list.png")
    menu = window.batch_list_view.item_menu(1)
    menu.popup(window.lists_tabs.mapToGlobal(QPoint(0, 0)))
    wait(0.3)
    save(menu.grab(), "batch_mode_menu.png")
    menu.hide()
    window.lists_tabs.setCurrentWidget(window.cut_list_page)

    # §8 バッチ追加の保存名称のダイアログ
    item = window.batch_manager.items[0]
    grab_shown(output_name_dialog.OutputNameDialog(item.output_path.with_name(f"{item.file_info.file_path.stem}_第1話.ts"),
                                                   item.file_info.file_path, tr("menu.add_to_batch"), window),
               "output_name_dialog.png")

    # §31.6 自動カットの案(青)と承認・棄却。テスト映像には番組情報が無いので、
    # 実際の録画で出る形の案をここで入れる
    from ts_smartcut.models import CutProposal

    window.open_file(video1)
    wait(2.5)
    fps = window.file_info.fps
    total = window.file_info.total_frames
    start, cm_start, cm_end = round(5 * fps), round(CM_START * fps), round(CM_END * fps)
    first = tr("reasons.seconds", value=f"{CM_START - 5:.0f}")
    second = tr("reasons.seconds", value=f"{SECONDS - CM_END:.0f}")
    cm = tr("reasons.multiple_15", length=tr("reasons.seconds", value="30"), count=2)
    window._on_proposals_ready(window._program_info_request, [
        CutProposal(start, cm_start - 1, 0.9, " / ".join([
            tr("reasons.program", length=first, reason=tr("reasons.logo_yes", logo="0.85", length=first)),
            tr("reasons.before", reason=tr("reasons.margin", where=tr("reasons.before_start"))),
            tr("reasons.after", reason=tr("reasons.logo_no", logo="0.02", length=cm))])),
        CutProposal(cm_end, total, 0.7, " / ".join([
            tr("reasons.program", length=second, reason=tr("reasons.logo_yes", logo="0.62", length=second)),
            tr("reasons.before", reason=tr("reasons.logo_no", logo="0.02", length=cm))])),
    ], "")
    window.cut_list.setCurrentItem(window.cut_list.topLevelItem(0))
    window.preview_widget.seek_frame(round((CM_START - 3) * fps))
    wait(4)
    window._poll_position()
    wait(0.5)
    save(window.grab(), "autocut_proposals.png")

    # §4.11 読み込み中のポップアップ(案の準備中。段階と進み具合はここで入れる)
    window._begin_loading()
    window._set_loading_stage("proposals", tr("proposals.preparing_percent", percent=42), 42)
    wait(0.5)
    save(window._loading_dialog.grab(), "loading_popup.png")
    window._end_loading()

    window.close()
    wait(0.5)
    shutil.rmtree(WORK, ignore_errors=True)  # テスト映像と一時設定
    print(f"完了: {OUT}")
    sys.stdout.flush()
    os._exit(0)  # mpvの終了処理を待たない(conftest.pyと同じ理由)


if __name__ == "__main__":
    sys.exit(main())
