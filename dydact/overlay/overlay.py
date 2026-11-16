"""Transparent, click-through, always-on-top overlay.

Thread-safety: call show_box / show_pointer / clear from ANY thread. They emit
Qt signals, so drawing always happens on the GUI thread (your API/voice
threads will need this).
"""

from __future__ import annotations

import ctypes
import logging
import math
import time
from ctypes import wintypes

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..vision.coords import Rect

log = logging.getLogger(__name__)

ACCENT = QColor("#7C5CFF")

# Win32: re-assert topmost position. Qt's own WindowStaysOnTopHint is a one-time flag -
# it doesn't survive another topmost window (e.g. a native Illustrator menu) later
# re-asserting ITS position, which otherwise ends up drawn above our overlay.
_HWND_TOPMOST = -1
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 version 2004 and newer


def _force_topmost(widget: QWidget):
    try:
        hwnd = int(widget.winId())
        set_pos = ctypes.windll.user32.SetWindowPos
        set_pos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        set_pos.restype = wintypes.BOOL
        set_pos(
            hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
        )
    except Exception:
        log.debug("Could not raise overlay to topmost position", exc_info=True)


def exclude_from_capture(widget: QWidget) -> bool:
    """Make a window invisible to screenshots/screen capture. Then Dydact does not have to hide
    its own windows and wait for Windows to repaint before every screenshot (saves ~150 ms a step).
    Call after the widget has been shown. Returns False if Windows refused (old version, etc.);
    callers should then keep the hide-and-wait path."""
    try:
        return bool(
            ctypes.windll.user32.SetWindowDisplayAffinity(
                int(widget.winId()), _WDA_EXCLUDEFROMCAPTURE
            )
        )
    except Exception:
        log.debug("exclude_from_capture failed; using existing fallback", exc_info=True)
        return False


class FlyoutRowOverlay(QWidget):
    """Independent, nonactivating native tooltip-sized surface above a popup row."""

    def __init__(self, screen=None):
        super().__init__(
            None,
            Qt.ToolTip
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._screen = screen
        self._stack_timer = QTimer(self, interval=30)
        self._stack_timer.timeout.connect(self._restack)

    def show_row(self, row):
        d = self._screen.devicePixelRatio() if self._screen else 1.0
        origin = self._screen.geometry().topLeft() if self._screen else QPointF(0, 0)
        self.setGeometry(
            round(origin.x() + row.x / d),
            round(origin.y() + row.y / d),
            max(1, math.ceil(row.w / d)),
            max(1, math.ceil(row.h / d)),
        )
        # Show only after the flyout is open. No activation, ownership change or mouse grab.
        self.show()
        exclude_from_capture(self)
        self._restack()
        self._stack_timer.start()
        self.update()

    def _restack(self):
        if self.isVisible():
            _force_topmost(self)

    def clear(self):
        self._stack_timer.stop()
        self.hide()

    def closeEvent(self, event):
        self._stack_timer.stop()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        fill = QColor(ACCENT)
        fill.setAlpha(45)
        painter.setBrush(fill)
        painter.setPen(QPen(ACCENT, 2))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 3, 3)


class OverlayWindow(QWidget):
    _box_sig = Signal(object, str, int, object)  # Rect, label, duration_ms
    _pointer_sig = Signal(float, float, str, int)
    _clear_sig = Signal()

    def __init__(self, screen=None):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # no taskbar entry
            | Qt.WindowTransparentForInput  # clicks pass through
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # never steal focus
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._row_overlay = FlyoutRowOverlay(screen)
        self._screen = screen
        self._dpr = screen.devicePixelRatio() if screen else 1.0
        if screen:
            self.setGeometry(screen.geometry())

        self._box: Rect | None = None
        self._box_label = ""
        self._flyout = None
        self._pointer: tuple[float, float] | None = None
        self._pointer_label = ""

        self._anim = QTimer(self, interval=16)
        self._anim.timeout.connect(self._tick)
        self._expire = QTimer(self, singleShot=True)
        self._expire.timeout.connect(self._do_clear)

        self._box_sig.connect(self._do_box)
        self._pointer_sig.connect(self._do_pointer)
        self._clear_sig.connect(self._do_clear)

    # ---- public, thread-safe API (screenshot-pixel coordinates) ----
    def show_box(
        self, rect: Rect, label: str = "", duration_ms: int = 0, *, flyout=None
    ):
        self._box_sig.emit(rect, label, duration_ms, flyout)

    def show_pointer(self, x: float, y: float, label: str = "", duration_ms: int = 0):
        self._pointer_sig.emit(x, y, label, duration_ms)

    def clear(self):
        self._clear_sig.emit()

    # ---- GUI-thread slots ----
    @Slot(object, str, int, object)
    def _do_box(self, rect, label, duration_ms, flyout=None):
        self._row_overlay.clear()
        if flyout is not None:
            self._do_clear()
            self._row_overlay.show_row(rect)
            if duration_ms > 0:
                self._expire.start(duration_ms)
            return
        self._flyout = None
        self._box, self._box_label = rect, label
        _force_topmost(self)
        self._start(duration_ms)

    @Slot(float, float, str, int)
    def _do_pointer(self, x, y, label, duration_ms):
        self._row_overlay.clear()
        self._pointer, self._pointer_label = (x, y), label
        _force_topmost(self)
        self._start(duration_ms)

    @Slot()
    def _do_clear(self):
        self._row_overlay.clear()
        self._box = self._pointer = None
        self._flyout = None
        self._anim.stop()
        self._expire.stop()
        self.update()

    def _start(self, duration_ms: int):
        # The pulsing pointer needs smooth frames; a static box only needs to keep re-asserting
        # topmost, so 10 fps is plenty and saves a repaint of the whole screen 60 times a second.
        self._anim.start(16 if self._pointer else 100)
        if duration_ms > 0:
            self._expire.start(duration_ms)
        else:
            self._expire.stop()
        self.update()

    def _tick(self):
        # Native Windows menus belong to a more aggressive topmost layer and
        # re-assert their own z-order on every repaint (e.g. on hover), which
        # wins against a one-shot SetWindowPos call almost immediately. So
        # re-assert ours repeatedly too, for as long as something is shown.
        _force_topmost(self)
        self.update()

    def closeEvent(self, event):
        self._do_clear()
        self._row_overlay.close()
        super().closeEvent(event)

    # ---- painting ----
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = self._dpr
        if self._box:
            b = self._box
            r = QRectF(b.x / d, b.y / d, b.w / d, b.h / d)
            fill = QColor(ACCENT)
            fill.setAlpha(40)
            p.setBrush(fill)
            p.setPen(QPen(ACCENT, 3))
            p.drawRoundedRect(r, 6, 6)
            if self._box_label:
                if self._flyout:
                    # Keep guidance outside the popup even when its native surface covers us.
                    popup = self._flyout
                    right = (popup.x + popup.w) / d + 14
                    label_width = (
                        p.fontMetrics().horizontalAdvance(self._box_label) + 60
                    )
                    left = popup.x / d - label_width - 14
                    x = right if right + label_width < self.width() else max(0, left)
                    y = max(40, min(self.height() - 10, r.center().y()))
                    self._pill(p, self._box_label, QPointF(x, y))
                    p.setPen(QPen(ACCENT, 3))
                    p.drawLine(
                        QPointF(x if x > r.right() else x + label_width, y), r.center()
                    )
                else:
                    self._pill(p, self._box_label, QPointF(r.left(), r.top() - 8))
        if self._pointer:
            cx, cy = self._pointer[0] / d, self._pointer[1] / d
            phase = (time.monotonic() * 1.6) % 1.0  # 0..1 pulse
            ring = 14 + 22 * phase
            pulse = QColor(ACCENT)
            pulse.setAlpha(int(200 * (1 - phase)))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(pulse, 3))
            p.drawEllipse(QPointF(cx, cy), ring, ring)
            p.setBrush(ACCENT)
            p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(QPointF(cx, cy), 7, 7)
            if self._pointer_label:
                self._pill(p, self._pointer_label, QPointF(cx + 18, cy - 30))

    @staticmethod
    def _pill(p: QPainter, text: str, anchor: QPointF):
        p.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        m = p.fontMetrics()
        w, h = m.horizontalAdvance(text) + 20, m.height() + 10
        r = QRectF(anchor.x(), anchor.y() - h, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 20, 28, 230))
        p.drawRoundedRect(r, h / 2, h / 2)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignCenter, text)
