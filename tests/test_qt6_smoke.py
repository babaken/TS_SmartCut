"""Phase 6a: Qt6基盤導入の疎通確認。

PySide6のQApplication/QOpenGLWidgetがこの実行環境で実際に有効な
OpenGLコンテキストを取得できることを検証する（Phase 6bのmpv render API
統合、Phase 7のプレビュー描画の前提条件）。
"""

from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QMainWindow

from tests._qt_helpers import pump_events


class _GLProbeWidget(QOpenGLWidget):
    def __init__(self):
        super().__init__()
        self.gl_info = None

    def initializeGL(self):
        ctx = self.context()
        fmt = ctx.format()
        self.gl_info = {
            "valid": ctx.isValid(),
            "version": fmt.version(),
            "is_opengles": ctx.isOpenGLES(),
        }

    def paintGL(self):
        functions = self.context().functions()
        functions.glClearColor(0.1, 0.4, 0.7, 1.0)
        functions.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT


def test_qapplication_starts(qapp):
    assert qapp is not None
    assert qapp.platformName()  # 何らかのQPAプラットフォームが選択されている


def test_qopenglwidget_gets_valid_context(qapp):
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)

    window = QMainWindow()
    widget = _GLProbeWidget()
    window.setCentralWidget(widget)
    window.resize(320, 240)
    window.show()

    try:
        pump_events(qapp, lambda: widget.gl_info is not None)

        assert widget.gl_info is not None, "initializeGL()が呼ばれなかった"
        assert widget.gl_info["valid"] is True
        assert widget.gl_info["version"][0] >= 3
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        qapp.processEvents()


def test_qopenglwidget_renders_and_is_grabbable(qapp):
    window = QMainWindow()
    widget = _GLProbeWidget()
    window.setCentralWidget(widget)
    window.resize(320, 240)
    window.show()

    try:
        pump_events(qapp, lambda: widget.gl_info is not None)
        pixmap = window.grab()
        assert not pixmap.isNull()
        assert pixmap.size().width() > 0
        assert pixmap.size().height() > 0
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        qapp.processEvents()
