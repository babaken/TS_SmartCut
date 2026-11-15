"""音声を選んだ後にウィンドウを閉じても落ちないか(test_audio_selection の子プロセスで動かす。落ちるとプロセスごと
終わるので、終了コードで見る)。引数: 音声が 2 つの TS、設定フォルダ。"""

import os
import sys
from pathlib import Path


def main() -> int:
    os.environ["TS_SMARTCUT_CONFIG_DIR"] = sys.argv[2]
    from PySide6.QtWidgets import QApplication

    from tests._qt_helpers import pump_events
    from tests._two_audio import SECOND_PID
    from ts_smartcut.gui.main_window import MainWindow
    from ts_smartcut.gui.preview_widget import PreviewWidget

    PreviewWidget.mpv_options = {"ao": "null"}
    app = QApplication([])
    window = MainWindow()
    window.show()
    pump_events(app, lambda: window.preview_widget.is_connected, timeout_ms=5000)
    window.open_file(Path(sys.argv[1]))
    pump_events(app, lambda: not window._loading, timeout_ms=10000)
    controller = window.preview_widget.controller
    controller.set_audio_pid(SECOND_PID)
    # 2 つ目の音声は番組の途中の PMT で加わる。track-list が変わって選び直すまで待つ
    pump_events(app, lambda: any(t.get("src-id") == SECOND_PID and t.get("selected")
                                 for t in controller._mpv.track_list or []), timeout_ms=10000)
    window.close()
    app.processEvents()
    print("closed" if controller._mpv is None else "mpv still open", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
