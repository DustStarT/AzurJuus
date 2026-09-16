from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QEvent, QUrl, Signal, QTimer
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QLabel,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from server import HOST, PORT, SETTINGS, create_server

WINDOW_TITLE = "AzurJuus"
WINDOW_MIN_WIDTH = 820
WINDOW_MIN_HEIGHT = 560
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
HEALTH_TIMEOUT = 8.0
WINDOW_MARGIN = 14
CONTROL_BUTTON_SIZE = 32
DRAG_STRIP_HEIGHT = 24
WINDOW_PRESETS = {
    "compact": (1024, 680),
    "balanced": (1280, 800),
    "expanded": (1480, 920),
}
APPDATA_ROOT = SETTINGS.workspace_state_path.resolve().parent
WEB_PROFILE_DIR = APPDATA_ROOT / "web-profile"
WEB_CACHE_DIR = APPDATA_ROOT / "web-cache"


def start_local_server(host: str = HOST, port: int = PORT):
    runtime_context: dict = {}
    server = create_server(host=host, port=port, runtime_context=runtime_context)
    thread = threading.Thread(target=server.serve_forever, name="AzurJuusLocalServer", daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    base_url = f"http://{actual_host}:{actual_port}"
    return server, thread, base_url, runtime_context


def stop_local_server(server, thread=None):
    with suppress(Exception):
        server.shutdown()
    if thread is not None:
        thread.join(timeout=20)
        if thread.is_alive():
            return False
    with suppress(Exception):
        server.server_close()
    return True


def wait_for_server(base_url: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    health_url = f"{base_url}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except urllib.error.URLError:
            time.sleep(0.15)
    return False


def create_application() -> QApplication:
    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    app.setOrganizationName("AzurPort")
    app.setOrganizationDomain("local.azurjuus")
    APPDATA_ROOT.mkdir(parents=True, exist_ok=True)
    return app


class DragStrip(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setCursor(Qt.OpenHandCursor)
        self._dragging = False
        self._offset = QPoint()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging and event.buttons() & Qt.LeftButton:
            window = self.window()
            if not window.isMaximized():
                window.move(event.globalPosition().toPoint() - self._offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class AzurJuusWindow(QMainWindow):
    resolution_requested = Signal(str)
    shutdown_finished = Signal(bool)
    def __init__(self, base_url: str, server, server_thread, runtime_context: dict):
        super().__init__()
        self.base_url = base_url
        self._server = server
        self._server_thread = server_thread
        self._runtime_context = runtime_context
        self._container = QWidget(self)
        self._stage = QWidget(self._container)
        self._view = QWebEngineView(self._stage)
        self._profile = self._create_web_profile()
        self._page = QWebEnginePage(self._profile, self._view)
        self._drag_strip = DragStrip(self)
        self._controls = QWidget(self)
        self._current_preset = "balanced"
        self._closing = False
        self._shutdown_complete = False
        self.shutdown_finished.connect(self._finish_shutdown)

        self._view.setPage(self._page)
        self._page.setBackgroundColor(QColor("#eef4fa"))
        self._view.setZoomFactor(1.0)
        self._page.loadFinished.connect(lambda _ok: self._view.setZoomFactor(1.0))

        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self._setup_shell()
        self._setup_controls()
        self._load_page()

        self.resolution_requested.connect(self.apply_resolution_preset)
        self._runtime_context["set_window_preset"] = self.resolution_requested.emit

    def _create_web_profile(self) -> QWebEngineProfile:
        WEB_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        WEB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        profile = QWebEngineProfile("AzurJuusDesktop", self)
        profile.setPersistentStoragePath(str(WEB_PROFILE_DIR))
        profile.setCachePath(str(WEB_CACHE_DIR))
        profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)

        settings = profile.settings()
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, False)
        return profile

    def _setup_shell(self):
        self._container.setObjectName("windowRoot")
        self._stage.setObjectName("windowStage")
        self._container.setAttribute(Qt.WA_StyledBackground, True)
        self._stage.setAttribute(Qt.WA_StyledBackground, True)

        root_layout = QVBoxLayout(self._container)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        stage_layout = QVBoxLayout(self._stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        stage_layout.addWidget(self._view)
        root_layout.addWidget(self._stage)
        self.setCentralWidget(self._container)

        self._view.setStyleSheet("background: #eef4fa; border: none;")

        self.setStyleSheet(
            """
            QMainWindow {
              background: #eef4fa;
            }
            QWidget#windowRoot {
              background: #eef4fa;
            }
            QWidget#windowStage {
              background: #eef4fa;
            }
            """
        )

    def _setup_controls(self):
        self._controls.setAttribute(Qt.WA_StyledBackground, True)
        self._controls.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(self._controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        for label, role, handler in [
            ("_", "minimize", self.showMinimized),
            ("[]", "maximize", self._toggle_maximized),
            ("X", "close", self.close),
        ]:
            button = QPushButton(label, self._controls)
            button.setProperty("windowRole", role)
            button.setFixedSize(CONTROL_BUTTON_SIZE, CONTROL_BUTTON_SIZE)
            button.clicked.connect(handler)
            layout.addWidget(button)

        self._controls.setStyleSheet(
            """
            QPushButton {
              border: none;
              border-radius: 16px;
              background: rgba(255, 255, 255, 0.84);
              color: #446074;
              font-size: 15px;
              font-family: Segoe UI, Microsoft YaHei UI, sans-serif;
            }
            QPushButton:hover {
              background: rgba(255, 255, 255, 0.98);
            }
            QPushButton[windowRole='close']:hover {
              background: rgba(255, 107, 121, 0.92);
              color: white;
            }
            QPushButton[windowRole='maximize']:hover,
            QPushButton[windowRole='minimize']:hover {
              background: rgba(151, 227, 255, 0.96);
              color: #173c58;
            }
            """
        )

    def _load_page(self):
        self._view.setUrl(QUrl(self.base_url))

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def apply_resolution_preset(self, preset: str):
        preset = preset if preset in WINDOW_PRESETS else "balanced"
        self._current_preset = preset
        if self.isMaximized():
            return
        width, height = WINDOW_PRESETS[preset]
        width = max(width, WINDOW_MIN_WIDTH)
        height = max(height, WINDOW_MIN_HEIGHT)
        self.resize(width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlays()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_overlays()

    def _position_overlays(self):
        top = 8
        left = 20
        width = max(200, self.width() - 240)
        self._drag_strip.setGeometry(left, top, width, DRAG_STRIP_HEIGHT)

        self._controls.adjustSize()
        controls_x = self.width() - self._controls.width() - 20
        self._controls.move(controls_x, top)
        self._controls.raise_()
        self._drag_strip.raise_()

    def event(self, event):
        if event.type() == QEvent.WindowStateChange:
            self._position_overlays()
        return super().event(event)

    def closeEvent(self, event):
        if self._shutdown_complete:
            super().closeEvent(event)
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self._runtime_context.pop("set_window_preset", None)
        self._controls.setEnabled(False)
        self._closing_label = QLabel("正在保存任务并退出…", self)
        self._closing_label.setAlignment(Qt.AlignCenter)
        self._closing_label.setStyleSheet("background: #eef4fa; color: #446074; font-size: 18px;")
        self._closing_label.setGeometry(self.rect())
        self._closing_label.show()
        self._closing_label.raise_()
        # Unload the page so WebSocket/fetch requests cannot hold the server open.
        self._view.stop()
        self._view.setUrl(QUrl("about:blank"))
        def stop():
            result = stop_local_server(self._server, self._server_thread)
            self.shutdown_finished.emit(result)
        self._shutdown_thread = threading.Thread(target=stop, name="AzurJuusShutdown", daemon=True)
        self._shutdown_thread.start()

    def _finish_shutdown(self, stopped):
        if not stopped:
            self._closing_label.setText("任务已保存，正在等待本地操作退出…")
            # Keep pumping Qt events while native tools finish; never join on Qt.
            QTimer.singleShot(250, self._check_shutdown)
            return
        self._shutdown_complete = True
        self._page.deleteLater()
        # The profile is parent-owned and must outlive its page.
        QTimer.singleShot(0, self.close)

    def _check_shutdown(self):
        if self._server_thread.is_alive():
            QTimer.singleShot(250, self._check_shutdown)
        else:
            self._server.server_close()
            self._finish_shutdown(True)


def run_check() -> int:
    server, thread, base_url, _runtime_context = start_local_server(port=0)
    try:
        ready = wait_for_server(base_url)
        print(f"DESKTOP_CHECK_URL={base_url}")
        print(f"DESKTOP_CHECK_READY={ready}")
        return 0 if ready else 1
    finally:
        stop_local_server(server, thread)


def run_desktop() -> int:
    app = create_application()
    try:
        server, thread, base_url, runtime_context = start_local_server()
    except OSError:
        QMessageBox.critical(None, WINDOW_TITLE, f"AzurJuus is already using port {PORT}, or the port is occupied by another app.")
        return 1
    if not wait_for_server(base_url):
        stop_local_server(server, thread)
        QMessageBox.critical(None, WINDOW_TITLE, "AzurJuus local service failed to start. Please try again.")
        return 1

    window = AzurJuusWindow(base_url, server, thread, runtime_context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(run_check())
    raise SystemExit(run_desktop())
