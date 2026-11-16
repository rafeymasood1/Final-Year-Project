"""Global mouse click/hold detection, so a step can confirm itself instead of
requiring the user to type 'next'. Needs pynput: the overlay is click-through,
so Qt never sees these events; pynput watches the whole screen on its own
thread. Everything crosses to the GUI thread via a Qt signal (same pattern as
overlay.py's _box_sig)."""

from __future__ import annotations

import time

from pynput import mouse
from PySide6.QtCore import QObject, Qt, QTimer, Signal

from ..vision.coords import Rect

HOLD_MS = 350  # press-and-hold vs a quick click (matches the long_press gesture)


class ClickWatcher(QObject):
    """arm(rect) before showing a step's box; disarm() when it's cleared.
    Normal steps fire on press. Hold steps fire at HOLD_MS while still down.
    Flyout items explicitly opt into release, including drag from their opener."""

    clicked_in = Signal(bool)
    _mouse_event = Signal(float, float, object, bool)

    def __init__(self, origin=(0, 0)):
        super().__init__()
        self.origin = origin
        self._rect: Rect | None = None
        self._down_at: float | None = None
        self._hold_required = False
        self._pressed = False
        self._release_required = False
        self._hold_timer = QTimer(self, singleShot=True)
        self._hold_timer.timeout.connect(self._held)
        self._mouse_event.connect(self._handle_click, Qt.QueuedConnection)
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()

    def arm(self, rect: Rect, *, hold=False, release=False):
        self._hold_timer.stop()
        self._rect = rect
        self._release_required = release
        self._hold_required = hold
        self._down_at = None

    def disarm(self):
        self._hold_timer.stop()
        self._rect = None
        self._down_at = None

    def _on_click(self, x, y, button, pressed):
        self._mouse_event.emit(x, y, button, pressed)

    def _handle_click(self, x, y, button, pressed):
        if button != mouse.Button.left:
            return
        x, y = x - self.origin[0], y - self.origin[1]
        if pressed:
            if self._pressed:
                return
            self._pressed = True
            if self._rect is None or not self._rect.contains(x, y):
                return
            if self._release_required:
                return
            if self._hold_required:
                self._down_at = time.monotonic()
                self._hold_timer.start(HOLD_MS)
            else:
                self.disarm()
                self.clicked_in.emit(False)
        else:
            was_pressed = self._pressed
            self._pressed = False
            self._hold_timer.stop()
            if self._release_required:
                if was_pressed and self._rect is not None and self._rect.contains(x, y):
                    self.disarm()
                    self.clicked_in.emit(False)
                return
            # A short hold reports feedback but leaves this step armed for retry.
            if self._down_at is not None:
                self._down_at = None
                self.clicked_in.emit(False)

    def _held(self):
        if self._pressed and self._rect is not None and self._down_at is not None:
            position = mouse.Controller().position
            if self._rect.contains(
                position[0] - self.origin[0], position[1] - self.origin[1]
            ):
                self.disarm()
                self.clicked_in.emit(True)

    def stop(self):
        self.disarm()
        self._listener.stop()
