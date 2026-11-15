"""アプリが異常終了した時に、起動したffmpegも終わらせる。

Windowsでは、アプリ(python.exe)をタスクマネージャー等で強制終了しても子の
ffmpegは動き続け、一時ファイルを書き続ける。書き込み中のファイルはWindowsでは
削除できないため、次の処理の「前回の一時ファイルの削除」で数GBが残った
(2026-10-05、Windows実機)。ffmpegを「閉じると中のプロセスを終了させる」
ジョブオブジェクトに入れておくと、アプリのプロセスが消えた時(ハンドルをOSが
閉じた時)にOSがffmpegも終了させる。

Linuxでも、アプリを強制終了(kill -9等)すると子のffmpegは最後まで動き続け、
CPUを使い、削除済みの一時ファイルに書き続けて容量を戻さなかった(2026-10-06)。
Linuxは開いているファイルも削除できるので一時ファイルは残らないが、ffmpegは
`setpriv --pdeathsig KILL` を通して起動し、親が終わるとOSがSIGKILLを送るように
する(launch_prefix)。preexec_fnでprctlを呼ぶ方法は、スレッドを使うアプリでは
子の起動が止まる恐れがあるため使わない。注意: pdeathsigは「起動したスレッド」の
終了で送られる。run()は起動したスレッドでffmpegの終了を待つので問題ない。
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_job: Optional[int] = None
_job_failed = False


def _create_job() -> Optional[int]:
    import ctypes
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit), ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    job_object_extended_limit_information = 9
    if not kernel32.SetInformationJobObject(
        job, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        return None
    return job  # プロセスが終わるまで閉じない(閉じるとffmpegが終了する)


def end_with_this_process(process: subprocess.Popen) -> None:
    """processを、このプロセスが終わると(異常終了でも)OSが終了させるようにする。
    Windows以外と、設定できなかった場合は何もしない(処理自体は続ける)。"""
    global _job, _job_failed
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        with _lock:
            if _job is None and not _job_failed:
                _job = _create_job()
                _job_failed = _job is None
            job = _job
        if job is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(job, int(process._handle)):  # type: ignore[attr-defined]
            logger.debug("Could not put ffmpeg into the job object: %d", ctypes.get_last_error())
    except (OSError, AttributeError, ValueError) as e:
        logger.debug("Could not put ffmpeg into the job object: %s", e)


@functools.lru_cache(maxsize=None)
def launch_prefix() -> List[str]:
    """ffmpegのコマンドの前に付けるもの。Linuxで setpriv があれば、親(このプロセス)が
    終わった時に子へSIGKILLを送らせる。それ以外は何も付けない。"""
    if not sys.platform.startswith("linux"):
        return []
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        logger.info("setpriv is missing, so ffmpeg cannot be stopped if the app crashes")
        return []
    return [setpriv, "--pdeathsig", "KILL", "--"]


def restore_library_path_for_children(environ=None) -> None:
    """.exe (PyInstaller) では、子のプロセスに PyInstaller の値を渡さない。Linux では LD_LIBRARY_PATH を起動の前の値に戻す。

    PyInstaller は同梱の部品 (_internal) を先に探すよう LD_LIBRARY_PATH を書き換え、元の値を
    LD_LIBRARY_PATH_ORIG に残す。そのまま子のプロセス (ffmpeg・ffprobe、「フォルダを開く」のファイルマネージャー)
    に引き継ぐと、子が同梱の古い部品を読んで動かないことがある (2026-11-15、Arch で、同梱の古い libstdc++ で
    ffmpeg の試しのエンコードが失敗し、ハードウェアエンコーダーが「無し」になった)。実行中のプロセスの探し場所は
    起動の時に決まるので、ここで戻してもアプリ自身には影響しない。起動の最初 (main) に 1 回呼ぶ。"""
    if not getattr(sys, "frozen", False):
        return
    env = os.environ if environ is None else environ
    # PyInstaller の起動部が起動の時に読むだけの値。子が PyInstaller のアプリだと、自分を起動し直した子と取り違える
    for name in [n for n in env if n.startswith("_PYI_")]:
        del env[name]
    if not sys.platform.startswith("linux"):
        return
    original = env.get("LD_LIBRARY_PATH_ORIG")
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)


def forget_bundled_qt_paths(environ=None) -> None:
    """.exe (PyInstaller) では、同梱の Qt の部品の探し場所を子のプロセスに渡さない。QApplication を作った後に呼ぶ。

    PySide6 の起動時フックが QT_PLUGIN_PATH・QML2_IMPORT_PATH を _internal に向ける。そのまま「フォルダを開く」の
    ファイルマネージャーなど Qt のアプリに渡ると、PC の Qt で動くそのアプリが同梱の版の違う Qt の部品 (Wayland の部品など)
    を読み込んでいた (2026-11-15、Arch の Dolphin)。アプリ自身の探し場所は、消す前に Qt に固定させておく
    (後から読み込む画像形式などの部品も同梱のものを使い続ける)。"""
    if not getattr(sys, "frozen", False):
        return
    env = os.environ if environ is None else environ
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.setLibraryPaths(QCoreApplication.libraryPaths())
    env.pop("QT_PLUGIN_PATH", None)
    env.pop("QML2_IMPORT_PATH", None)


def no_window() -> dict:
    """subprocess.run/Popenに渡す引数。Windowsでは子のコンソール窓を作らない。

    アプリはコンソール無しで動く(pythonw.exe・console=Falseの.exe)ため、
    ffmpeg/ffprobeを起動するたびに新しいコンソール窓が開いては閉じていた
    (2026-10-06、Windows実機)。出力はパイプで受け取るので窓は要らない。
    ts_smartcut内の起動はすべてこれを付ける(tests/test_child_processes.pyが確認)。"""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def kill_process_tree(process: subprocess.Popen) -> None:
    """processとその子プロセスを終了させる(中止ボタン用)。

    Windowsでは、PATH上のffmpegがChocolatey等の中継用exe(shim)で、本物のffmpegを
    子として起動していることがある。process.kill()は中継役だけを終了させ、本物は
    最後まで動き続けた(2026-10-06、Windows実機)ため、taskkill /T で子ごと終了させる。
    Linuxではffmpegを直接(setprivはexecで置き換わる)起動しているのでkillでよい。"""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True, timeout=10, **no_window(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("taskkill failed: %s", e)
    if process.poll() is None:
        process.kill()


class StalledProcessError(Exception):
    """run_until_stalled: 子プロセスが stall_seconds の間、何も出力しなかったので終了させた。"""

    def __init__(self, seconds: float, stderr: bytes):
        self.seconds = seconds
        self.stderr = stderr
        super().__init__(f"no output for {seconds:.0f} s")


def run_until_stalled(cmd: List[str], stall_seconds: float,
                      cancel_event: Optional[threading.Event] = None) -> subprocess.CompletedProcess:
    """cmd を実行し、標準出力と標準エラーを bytes で受け取る(subprocess.run の capture_output と同じ)。
    stall_seconds の間どちらにも何も出なければ、子プロセスごと終了させて StalledProcessError。
    cancel_event が立ったら子プロセスごと終了させて戻る(呼び出し側が cancel_event を見て中止にする)。

    ファイル全体を読む ffprobe(キーフレーム)が、録画の終わり近くで出力も CPU も止まったまま
    終わらず、読み込み中の画面が固まった(2026-10-12、ゲームセンターCX #170。ほかの ffprobe が
    同じ録画を読んでいる間だけ再現し、原因は不明)。全体の時間では長い録画や遅いディスクと
    見分けられないので、出力が止まった時間で判断する。"""
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window())
    end_with_this_process(process)
    last_output = [time.monotonic()]
    chunks: dict = {"out": [], "err": []}

    def pump(stream, key: str) -> None:
        while True:
            data = stream.read1(65536)
            if not data:
                return
            chunks[key].append(data)
            last_output[0] = time.monotonic()

    readers = [threading.Thread(target=pump, args=(process.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(process.stderr, "err"), daemon=True)]
    for reader in readers:
        reader.start()
    stalled = False
    while process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is None and cancel_event is not None and cancel_event.is_set():
            kill_process_tree(process)
            process.wait()
            break
        if process.poll() is None and time.monotonic() - last_output[0] > stall_seconds:
            stalled = True
            kill_process_tree(process)
            process.wait()
            break
    for reader in readers:
        reader.join(timeout=10)
    stderr = b"".join(chunks["err"])
    if stalled:
        logger.warning("Killed a child process that made no output for %.0f s: %s", stall_seconds, cmd[0])
        raise StalledProcessError(stall_seconds, stderr)
    return subprocess.CompletedProcess(cmd, process.returncode, b"".join(chunks["out"]), stderr)
