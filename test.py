"""Walk through SVG-backed Illustrator icons using Dydact's live guide.

Run with Illustrator visible on the primary monitor. Click the highlighted
icon to pass it. Follow any press-and-hold or menu steps the guide shows first.
Type retry to restart an icon or skip to record it as skipped and continue.
"""

from __future__ import annotations

import argparse
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from dydact.guide.guide import Guide
from dydact.overlay.overlay import OverlayWindow, exclude_from_capture
from dydact.paths import REFS, TEMPLATE_STATES, THEME_BACKGROUNDS, themed_pngs
from dydact.ui.frontend import PromptBox
from dydact.uigraph.graph import UIGraph
from dydact.vision.capture import ScreenCapture


def svg_backed_icons(graph: UIGraph) -> list[str]:
    """Use the themed renders to identify SVG-backed targets in the workspace."""
    return [
        icon
        for icon in graph.icon_names()
        if sum(1 for _ in themed_pngs(REFS / icon))
        == len(THEME_BACKGROUNDS) * len(TEMPLATE_STATES)
    ]


class IconTest(Guide):
    def __init__(self, overlay, box, capture, graph, icons):
        super().__init__(overlay, box, capture, graph)
        self.icons = icons
        self.index = 0
        self.passed = 0
        self.skipped = []
        self.box.submitted.disconnect(self.search)
        self.box.submitted.connect(self._command)
        self.box.input.setPlaceholderText("retry or skip")

    def start(self):
        # Wait for the same screenshot templates the live matcher will use.
        if not self._finder.done():
            QTimer.singleShot(50, self.start)
            return
        if self._finder.exception() is None:
            self._show_test()

    def _show_test(self):
        while self.index < len(self.icons):
            icon = self.icons[self.index]
            if icon in self._finder.result().refs:
                self.box.title.setText(
                    f"Icon {self.index + 1}/{len(self.icons)} · {icon}"
                )
                self.search(icon)
                return
            self.skipped.append(icon)
            self.index += 1
            print(f"SKIP {icon} (no screenshot template)", flush=True)

        self.box.title.setText("Icon test complete")
        summary = f"{self.passed} passed, {len(self.skipped)} skipped."
        self.box.set_status(summary, "ok")
        print(summary, flush=True)

    def _nav_hint(self):
        return "  [retry / skip]" if self.target is not None else ""

    def _on_box_clicked(self, held: bool):
        current = self.target
        super()._on_box_clicked(held)
        if current is not None and self.target is None:
            self.passed += 1
            self.index += 1
            print(f"PASS {current}", flush=True)
            QTimer.singleShot(0, self._show_test)

    def _command(self, text: str):
        if (
            self._busy
            or self.index >= len(self.icons)
            or not self._finder.done()
            or self._finder.exception()
        ):
            return
        command = text.strip().lower()
        if command == "retry":
            self.clear()
            self._show_test()
        elif command == "skip":
            self.clear()
            icon = self.icons[self.index]
            self.skipped.append(icon)
            self.index += 1
            print(f"SKIP {icon} (manual)", flush=True)
            self._show_test()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the icon queue without opening the guide",
    )
    args = parser.parse_args()
    graph = UIGraph.load()
    icons = svg_backed_icons(graph)
    if not icons:
        parser.error(f"No SVG-backed icon targets found in {REFS}")
    if args.list:
        print("\n".join(icons))
        return 0

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    ticker = QTimer(app, interval=250, timeout=lambda: None)
    ticker.start()

    screen = QGuiApplication.primaryScreen()
    overlay = OverlayWindow(screen)
    overlay.show()
    box = PromptBox(screen, suggestions=[])
    test = IconTest(overlay, box, ScreenCapture(monitor_index=1), graph, icons)
    box.quit_requested.connect(app.quit)
    app.aboutToQuit.connect(test.clicks.stop)
    box.show_mode("query")
    box.show()
    box.set_fast_capture_available(
        exclude_from_capture(overlay) and exclude_from_capture(box)
    )
    QTimer.singleShot(0, test.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
