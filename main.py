"""Dydact entry point.   Run:  python main.py

A round "D" button sits bottom-right above the taskbar. Click it for Course mode, Query mode and
Settings. This file only builds the Qt app and wires the pieces together; the logic lives in
dydact/ (see README.md for the map).
"""

from __future__ import annotations

import logging
import os
import signal
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from dydact.course.runner import CourseRunner
from dydact.guide.guide import Guide
from dydact.overlay.overlay import ACCENT, OverlayWindow, exclude_from_capture
from dydact.ui.frontend import PromptBox
from dydact.uigraph.graph import ContextError, UIGraph
from dydact.vision.capture import ScreenCapture


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(ACCENT)
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setBrush(QColor("white"))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pm)


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DYDACT_DEBUG") == "1" else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        graph = UIGraph.load()
    except ContextError as e:
        sys.exit(f"contexts.json problem: {e}")
    for p in graph.problems():
        print(
            f"[dydact] contexts.json [{p.code}] ({'error' if p.fatal else 'warning'}): {p.message}",
            flush=True,
        )

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGINT, lambda *_: app.quit())  # Ctrl+C works
    ticker = QTimer(interval=250, timeout=lambda: None)  # let Python see signals
    ticker.start()

    screen = QGuiApplication.primaryScreen()
    overlay = OverlayWindow(screen)
    overlay.show()
    cap = ScreenCapture(monitor_index=1)  # primary, same as the overlay

    box = PromptBox(screen, suggestions=graph.targets())
    box.quit_requested.connect(app.quit)
    guide = Guide(overlay, box, cap, graph)
    course = CourseRunner(box, guide, graph)  # noqa: F841 (keep a reference)
    app.aboutToQuit.connect(guide.clicks.stop)
    box.show()

    # Fast capture: if Windows lets us hide our windows from screenshots, no step needs the
    # hide-and-wait. If it refuses, Settings shows the option as unavailable and HIDE_MS is used.
    box.set_fast_capture_available(
        exclude_from_capture(overlay) and exclude_from_capture(box)
    )

    # The tray offers the same Quit Dydact action as the D menu.
    tray = QSystemTrayIcon(make_icon(), app)
    menu = QMenu()
    quit_action = QAction("Quit Dydact")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.setToolTip("Dydact")
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
