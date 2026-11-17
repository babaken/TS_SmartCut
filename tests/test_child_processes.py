"""アプリが強制終了されたら、起動したffmpegも終わること(system/child_processes.py)。

Windowsでは子プロセスは親が強制終了されても動き続け、書き込み中の一時ファイルを
消せなくなった(2026-10-05、Windows実機)。
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from ts_smartcut.system import child_processes

PARENT = r"""
import subprocess, sys, time
from ts_smartcut.system.child_processes import end_with_this_process
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
end_with_this_process(child)
print(child.pid, flush=True)
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, errors="replace")
    return str(pid) in out.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows専用(ジョブオブジェクト)")
def test_child_ends_when_parent_is_killed():
    parent = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline())
    assert _alive(child_pid)

    parent.kill()  # タスクマネージャーの「タスクの終了」と同じTerminateProcess
    parent.wait()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(child_pid):
        time.sleep(0.1)
    assert not _alive(child_pid)


def test_does_nothing_harmful_for_finished_process():
    from ts_smartcut.system.child_processes import end_with_this_process

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    end_with_this_process(process)  # 例外を出さない


# Linux: 強制終了したアプリのffmpegが最後まで動き続けた(2026-10-06)。ffmpegは
# setpriv --pdeathsig KILL を通して起動する
LINUX_PARENT = r"""
import os
from ts_smartcut.ffmpeg.wrapper import FFmpegWrapper
from ts_smartcut.models import AppSettings
FFmpegWrapper(AppSettings()).run(
    ["-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=600", "-f", "null", os.devnull])
"""


def _running_on_linux(pid: int) -> bool:
    """終了してゾンビになったプロセスは動いていないとみなす。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


# キーフレームの ffprobe・波形の ffmpeg は run_until_stalled で起動する。setpriv が付いておらず、
# アプリを強制終了すると、出力の止まった子(固まった ffprobe など)が残った(2026-11-17)。出力を続ける子は
# パイプが閉じて自分で終わるので、確かめでは何も出さない子にする(パイプに書くと不具合が隠れる)
LINUX_STALLED_PARENT = r"""
import os
from ts_smartcut.system.child_processes import run_until_stalled
run_until_stalled(["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=600",
                   "-f", "null", os.devnull], stall_seconds=60)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux専用(setpriv)")
@pytest.mark.parametrize("script", [LINUX_PARENT, LINUX_STALLED_PARENT], ids=["run", "run_until_stalled"])
def test_ffmpeg_ends_when_the_app_is_killed_on_linux(script):
    from ts_smartcut.system.child_processes import launch_prefix

    if not launch_prefix():
        pytest.skip("setpriv が無い環境")
    parent = subprocess.Popen([sys.executable, "-c", script])
    deadline = time.monotonic() + 10
    children = []
    while time.monotonic() < deadline and not children:
        out = subprocess.run(["pgrep", "-P", str(parent.pid)], capture_output=True, text=True).stdout
        children = [int(x) for x in out.split()]
        time.sleep(0.1)
    assert children, "ffmpegが起動しなかった"

    parent.kill()  # kill -9 と同じ
    parent.wait()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_running_on_linux(pid) for pid in children):
        time.sleep(0.1)
    left = [pid for pid in children if _running_on_linux(pid)]
    for pid in left:  # 失敗した時も、残った ffmpeg を動かしたままにしない
        os.kill(pid, signal.SIGKILL)
    assert left == []


def test_every_launch_in_the_app_hides_the_console_window():
    # アプリはコンソール無しで動くので、no_window()を付けない起動はWindowsで
    # コンソール窓を開く(2026-10-06)。ts_smartcut内の起動すべてに付いているか。
    import ast
    from pathlib import Path

    import ts_smartcut

    missing = []
    for path in Path(ts_smartcut.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in ("run", "Popen", "call", "check_call", "check_output")
            ):
                spread = [
                    k.value for k in node.keywords
                    if k.arg is None and isinstance(k.value, ast.Call)
                    and getattr(k.value.func, "id", None) == "no_window"
                ]
                if not spread:
                    missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windowsのコンソール窓の確認")
def test_no_window_starts_the_child_without_a_console(tmp_path):
    # アプリと同じくコンソールの無い親(pythonw.exe)から起動する。親にコンソールが
    # あると子はそれを共有するので、フラグが無くても窓は開かず確認にならない。
    # (フラグ無しでは子のGetConsoleWindow()が0以外=新しい窓、を手で確認済み。
    # テストでは窓を開かないよう、フラグ付きだけを確かめる)
    from pathlib import Path

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pytest.skip("pythonw.exe がありません")
    out = tmp_path / "console.txt"
    parent = "\n".join([
        "import subprocess",
        "from ts_smartcut.system.child_processes import no_window",
        "code = 'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())'",
        f"r = subprocess.run([{str(sys.executable)!r}, '-c', code], capture_output=True, text=True, **no_window())",
        f"open({str(out)!r}, 'w').write(r.stdout.strip())",
    ])
    root = Path(__file__).resolve().parent.parent
    subprocess.run([str(pythonw), "-c", parent], cwd=root, timeout=30)
    assert out.read_text() == "0"


def test_run_until_stalled_kills_a_child_that_stops_making_output():
    """出力が止まったまま終わらない子は、止まった時間で終了させる(2026-10-12、キーフレームの ffprobe が
    録画の終わり近くで止まり、読み込みの画面が固まった)。"""
    from ts_smartcut.system.child_processes import StalledProcessError, run_until_stalled

    script = "import sys, time\nprint('first', flush=True)\ntime.sleep(60)\n"
    started = time.monotonic()
    with pytest.raises(StalledProcessError) as info:
        run_until_stalled([sys.executable, "-c", script], stall_seconds=1.5)
    assert time.monotonic() - started < 20
    assert info.value.seconds == 1.5


def test_run_until_stalled_waits_for_slow_but_steady_output():
    from ts_smartcut.system.child_processes import run_until_stalled

    script = (
        "import sys, time\n"
        "for i in range(5):\n"
        "    print(i, flush=True); time.sleep(0.6)\n"
        "sys.stderr.write('done')\n"
        "sys.exit(3)\n"
    )
    result = run_until_stalled([sys.executable, "-c", script], stall_seconds=1.5)
    assert result.returncode == 3
    assert result.stdout.split() == [b"0", b"1", b"2", b"3", b"4"]
    assert result.stderr == b"done"


# ---- Linux の .exe: 子のプロセスに同梱の部品を探させない ------------------------------------

def test_library_path_is_restored_for_children_of_the_linux_exe(monkeypatch):
    monkeypatch.setattr(child_processes.sys, "platform", "linux")
    monkeypatch.setattr(child_processes.sys, "frozen", True, raising=False)
    env = {"LD_LIBRARY_PATH": "/app/_internal:/opt/lib", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}
    child_processes.restore_library_path_for_children(env)
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"

    env = {"LD_LIBRARY_PATH": "/app/_internal"}  # 起動の前は無かった
    child_processes.restore_library_path_for_children(env)
    assert "LD_LIBRARY_PATH" not in env


def test_library_path_is_left_alone_outside_the_linux_exe(monkeypatch):
    env = {"LD_LIBRARY_PATH": "/x", "LD_LIBRARY_PATH_ORIG": "/y"}
    monkeypatch.setattr(child_processes.sys, "frozen", False, raising=False)
    monkeypatch.setattr(child_processes.sys, "platform", "linux")
    child_processes.restore_library_path_for_children(env)  # ソースから動かす時
    monkeypatch.setattr(child_processes.sys, "frozen", True, raising=False)
    monkeypatch.setattr(child_processes.sys, "platform", "win32")
    child_processes.restore_library_path_for_children(env)  # Windows の .exe
    assert env == {"LD_LIBRARY_PATH": "/x", "LD_LIBRARY_PATH_ORIG": "/y"}


def test_pyinstaller_values_are_not_passed_to_children(monkeypatch):
    monkeypatch.setattr(child_processes.sys, "platform", "win32")
    monkeypatch.setattr(child_processes.sys, "frozen", True, raising=False)
    env = {"_PYI_ARCHIVE_FILE": "x", "_PYI_PARENT_PROCESS_LEVEL": "1", "PATH": "p"}
    child_processes.restore_library_path_for_children(env)
    assert env == {"PATH": "p"}


def test_bundled_qt_paths_are_forgotten_after_qt_has_its_own(qapp, monkeypatch):
    from PySide6.QtCore import QCoreApplication

    paths = QCoreApplication.libraryPaths()
    env = {"QT_PLUGIN_PATH": "/app/_internal/PySide6/Qt/plugins", "QML2_IMPORT_PATH": "/q", "PATH": "p"}
    child_processes.forget_bundled_qt_paths(env)  # ソースから動かす時は何もしない
    assert "QT_PLUGIN_PATH" in env

    monkeypatch.setattr(child_processes.sys, "frozen", True, raising=False)
    child_processes.forget_bundled_qt_paths(env)
    assert env == {"PATH": "p"}
    assert QCoreApplication.libraryPaths() == paths  # アプリ自身の探し場所は変わらない
