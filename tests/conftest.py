import os
import subprocess
import sys
from pathlib import Path

import pytest


_session_exit_code: dict[str, int] = {}


def pytest_sessionfinish(session, exitstatus):
    """セッションの本来の終了コードだけを記録する（ここではまだ終了しない）。

    pytest_unconfigure（下記）でこの値を使ってos._exit()する。sessionfinish
    自体で即os._exit()すると、pytest本体の終了サマリ出力
    ("295 passed in X.Xs"等)を担うterminal reporterのpytest_sessionfinish
    (wrapperフックで、`yield`後にsummary_stats()を呼ぶ実装)より先にプロセスが
    死んでしまい、summary行が一切表示されなくなることを実機で確認した
    （wrapper=True/trylast=True等でフックの実行順を制御しようとしたが、
    hookwrapperの入れ子順を安定して外側に持ってくる方法が見つからなかった）。
    """
    _session_exit_code["code"] = int(exitstatus)


def pytest_unconfigure(config):
    """pytest_sessionfinish完了後・Pythonの通常終了処理が始まる前に呼ばれる
    （wrap_sessionのfinallyブロックでsessionfinishの直後に実行される）。

    Windows実機で判明した既知の問題(MPVController.close()に低確率の
    ネイティブクラッシュがある。CLAUDE.md「Testing approach」節参照)への
    対処。セッション最後のclose()呼び出しでこれが発生すると、Pythonの
    通常の終了処理(atexit等)の最中にクラッシュが起き、pytestプロセスが
    本来passしているのに偽のexit code 139を返してしまう。この時点で
    summary行はすでに出力済みなので、ここでos._exit()して通常の終了処理
    (atexit等、クラッシュの発生源)を丸ごとスキップし、pytestが確定させた
    本来のexitstatusをそのまま返すことでこれを回避する。ただしセッション
    途中のclose()で発生するケース(頻度は低い)は防げない。
    """
    code = _session_exit_code.get("code")
    if code is None:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        # os._exit()(ExitProcess)でもDLLの終了処理は走り、音声出力なし(ao=null)の
        # mpvで音声デバイス一覧を取った(設定画面を開いた)あとはそこで落ちた
        # (2026-10-05、Windows実機。ao=nullでないアプリ本体は正常に終了する)。
        # TerminateProcessはDLLの終了処理も飛ばす。
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)
    os._exit(code)


@pytest.fixture(scope="session", autouse=True)
def isolated_config_dir(tmp_path_factory):
    """設定ディレクトリ(settings.json/recent_files.json/cut_history.db/logs 等)をテスト用の
    一時ディレクトリへ向ける。MainWindowは既定で実ユーザーの設定ディレクトリの
    最近使用したファイル履歴を読み書きするため、テストが利用者の環境を
    汚さないようにする。アプリの設定は CONFIG_DIR_ENV で向ける(既定の場所を実行ファイルの
    フォルダへ移しても効く、設計書 §31.9)。XDG_CONFIG_HOME/APPDATA は mpv など外部の
    設定を読まないために向ける。"""
    from ts_smartcut.core.settings_manager import CONFIG_DIR_ENV

    config_dir = tmp_path_factory.mktemp("config")
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "APPDATA", CONFIG_DIR_ENV)}
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["APPDATA"] = str(config_dir)
    os.environ[CONFIG_DIR_ENV] = str(config_dir / "ts_smartcut")
    yield config_dir
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session", autouse=True)
def isolated_app_temp_dir(tmp_path_factory):
    """特徴の計算の一時フォルダ(既定は実行ファイルの直下の Temp/)をテスト用の
    一時ディレクトリへ向ける。"""
    from ts_smartcut.autocut import features

    temp_dir = tmp_path_factory.mktemp("app_temp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(features, "app_temp_dir", lambda: temp_dir)
        yield temp_dir


@pytest.fixture(scope="session", autouse=True)
def isolated_keyframe_cache_dir(tmp_path_factory):
    """キーフレームのキャッシュ(既定は実行ファイルの直下の KeyframeCache/)をテスト用の
    一時ディレクトリへ向ける。"""
    from ts_smartcut.ffmpeg import wrapper

    cache_dir = tmp_path_factory.mktemp("keyframe_cache")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wrapper, "keyframe_cache_dir", lambda: cache_dir)
        yield cache_dir


@pytest.fixture(scope="session", autouse=True)
def no_automatic_proposals():
    """録画を開いた時の自動カットの案(読み込みのポップアップで操作を止める、§4.11)を止める。
    テストの録画は案を作れないか時間がかかり、画面が止まったままだとボタンの操作が効かない。
    案と読み込みのポップアップは test_gui_proposals.py で個別に確かめる。"""
    from ts_smartcut.gui.main_window import MainWindow

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(MainWindow, "autocut_proposals", False)
        yield


@pytest.fixture(autouse=True)
def forget_ffmpeg_versions():
    """DependencyChecker が覚えた `ffmpeg -version` の出力を、差し替えたテストの後に残さない。"""
    yield
    from ts_smartcut.system.dependency_checker import DependencyChecker

    DependencyChecker.clear_ffmpeg_version_cache()


@pytest.fixture(autouse=True)
def default_language_after_each_test():
    """英語の MainWindow を作ったテストの後に、アプリ全体の言語(language_manager.tr)が英語のまま
    残らないようにする(ほかのテストは日本語の文字を見る)。"""
    yield
    from ts_smartcut.i18n import language_manager

    language_manager._current = None


@pytest.fixture(scope="session", autouse=True)
def output_name_dialog_accepts_the_default():
    """バッチ追加・名称変更で出る保存名称のダイアログ(2026-10-11)を、初期値のまま OK した
    ことにする(ダイアログを開くとテストが止まる)。初期値が誤りなら(check)キャンセル扱い。
    ダイアログそのものは test_gui_batch.py で確かめる(元の関数は REAL_ASK_OUTPUT_NAME)。"""
    from ts_smartcut.gui import output_name_dialog

    def accept_default(parent, initial, input_path, check, title=None):
        return None if check(initial) else initial

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(output_name_dialog, "REAL_ASK_OUTPUT_NAME", output_name_dialog.ask_output_name, raising=False)
        mp.setattr(output_name_dialog, "ask_output_name", accept_default)
        yield


@pytest.fixture(scope="session")
def qapp():
    """プロセス内で唯一のQApplicationインスタンスをセッション全体で共有する
    （Qtは1プロセスにつきQApplicationを1つしか許容しない）。Phase 6a以降の
    Qt6関連テストはこのフィクスチャを介してアプリを取得する。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="session", autouse=True)
def mpv_without_audio_output():
    """テスト中のmpvは音声出力なし(ao=null)で動かす。

    Windows実機で判明: 音声出力(WASAPI)を使ったmpvインスタンスを2つ以上
    生成・破棄した後にカラースキーム変更(QStyleHints.setColorScheme、
    ThemeManager.apply_themeが呼ぶ)を行うとアクセス違反でプロセスが落ちる。
    ao=nullなら起きない。アプリ本体はmpvを1インスタンスしか作らないため
    影響しない(ライブのウィンドウでのテーマ切替は実機で確認済み)が、
    テストはモジュールごとにインスタンスを作るため確実に踏む。
    volumeプロパティ等はao=nullでも機能する。"""
    from ts_smartcut.gui.preview_widget import PreviewWidget

    saved = PreviewWidget.mpv_options
    PreviewWidget.mpv_options = {"ao": "null"}
    yield
    PreviewWidget.mpv_options = saved


@pytest.fixture(scope="module")
def window(qapp):
    """モジュールごとに1つのMainWindow（したがって1つの実mpvインスタンス）
    を共有する。実機検証(Phase 6b, Linux)の教訓: 同一プロセス内でmpv
    インスタンスの生成・破棄を繰り返しても(旧wid方式のような)クラッシュは
    再発しないが、実mpv接続のコストを避けるためモジュール単位で使い回す
    （CLAUDE.mdのテスト方針）。scope="module"なのでtest_gui_main_window.py
    とtest_gui_cut_editing.py等、別モジュールはそれぞれ別インスタンスを持つ。

    Windows実機検証(Phase 10)で追加判明: MPVController.close()自体に
    低確率(数%程度)のネイティブクラッシュがある。「2個目以降のインスタンス
    でのみ起きる」決定論的な現象ではなく、1個目のclose()でも起こり得る
    確率的な現象と判明している。本フィクスチャがモジュール単位でしか
    close()しないのはこの現象を避けるためではなく(避けられない)、単に
    実mpv接続コストを避けるため。この既知の問題自体への対処は
    pytest_sessionfinish（本ファイル冒頭）を参照。"""
    from ts_smartcut.gui.main_window import MainWindow
    from tests._qt_helpers import pump_events

    w = MainWindow()
    w.show()
    pump_events(qapp, lambda: w.preview_widget.is_connected, timeout_ms=3000)
    yield w
    w.close()
    # C++側のQOpenGLWidget/QMainWindowの実削除をこの場で確定させる。
    # deleteLater()後にイベントループを回さないと削除が遅延し、後続の
    # テストモジュールで新規ウィジェットを表示した際にクラッシュする
    # ことを実機で確認した。
    w.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """3秒・25fps・GOP10の合成テスト動画（音声トラック付き）を生成する。
    実ファイルをリポジトリにコミットせず、実行時にffmpegで生成する。"""
    out_dir = tmp_path_factory.mktemp("ffmpeg_fixtures")
    path = out_dir / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10",
            "-c:a", "aac",
            "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(scope="session")
def interlaced_sample_video(tmp_path_factory) -> Path:
    """sample_video と同じ絵・長さで、インターレースとして符号化したもの。画面で開くテスト用:
    プログレッシブの録画は開く時に断る(E206、2026-11-17)。"""
    out_dir = tmp_path_factory.mktemp("ffmpeg_fixtures_interlaced")
    path = out_dir / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
            "-vf", "setfield=tff", "-flags", "+ilme+ildct",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10",
            "-c:a", "aac",
            "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path
