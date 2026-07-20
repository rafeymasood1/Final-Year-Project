"""
Live UI Automation Overlay Inspector - OPTIMIZED v4
---------------------------------------------------
v4: removed STEP mode entirely (task library, element-sequence matching,
pulsing single-target highlight, Tab/Space/Backspace step controls). This
is now search-only: type a query, press Enter, matching elements light up.

v3 fixed the "slow on window switch" problem: walk_ui_tree_fast() makes a
blocking COM/UIA call that can take anywhere from tens of ms to multiple
seconds on complex apps (browsers, IDEs, Office). That call now runs on a
background QThread with its own COM apartment
(auto.UIAutomationInitializerInThread), so the Qt UI thread never blocks -
window switches feel instant (title updates immediately; highlighted
elements populate a beat later once the scan finishes). Stale results
(from a window you've since switched away from) are dropped by comparing
the walked hwnd against the currently-tracked hwnd.

Kept from earlier versions:
1. UIA FindAll with cache request (single COM round-trip vs N recursive calls)
2. SIMD fuzzy search (rapidfuzz)
3. Preprocessed search cache
4. Dirty-region painting
5. Event-driven refresh (no polling)

Usage:
    python overlay_inspector_fast.py
    -> control box appears bottom-right, tracks focused window
    Type a query, press Enter to search. Esc = quit. F5 = rescan.
"""

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import psutil
import win32gui
import win32process
import uiautomation as auto
from rapidfuzz import fuzz, process

os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

# --------------------------------------------------------------------------
# Win32 constants
# --------------------------------------------------------------------------

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_VALUECHANGE = 0x800E
WINEVENT_OUTOFCONTEXT = 0x0000

user32 = ctypes.windll.user32

_WinEventProcType = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)

# --------------------------------------------------------------------------
# DPI awareness
# --------------------------------------------------------------------------

def _make_process_dpi_aware() -> None:
    for fn in (
        lambda: user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: user32.SetProcessDPIAware(),
    ):
        try:
            fn()
            return
        except Exception:
            continue


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class UIElement:
    left: int
    top: int
    right: int
    bottom: int
    control_type: str
    name: str
    hwnd: int = 0
    _search_key: str = field(default="", repr=False)

    def __post_init__(self):
        if not self._search_key:
            self._search_key = f"{self.name} {self.control_type}".strip().lower()

    @property
    def rect(self) -> QtCore.QRect:
        return QtCore.QRect(self.left, self.top, self.right - self.left, self.bottom - self.top)


SearchCache = List[Tuple[UIElement, str]]


# --------------------------------------------------------------------------
# FAST UIA tree walk using FindAll with cache request
# --------------------------------------------------------------------------

MAX_ELEMENTS = 5000


def walk_ui_tree_fast(hwnd: int, max_elements: int = MAX_ELEMENTS) -> List[UIElement]:
    """
    Optimized UIA tree walk using FindAll with property caching.

    Instead of recursive GetChildren() calls (N round-trips), we use FindAll
    which returns all descendants in a single COM operation with cached
    properties. This is 5-20x faster than the original recursive approach.

    NOTE: this function performs a blocking COM call and must only be
    invoked from a thread that has its own initialized COM apartment (see
    TreeWalkWorker below). Never call this directly from the Qt UI thread.
    """
    elements: List[UIElement] = []

    try:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return elements

        # Create a cache request to batch property fetches. Created fresh
        # per-call (and per-thread/apartment) intentionally - COM objects
        # must not be shared across threads without marshaling.
        cache_request = auto.CacheRequest()
        cache_request.AddProperty(auto.PropertyId.BoundingRectangleProperty)
        cache_request.AddProperty(auto.PropertyId.ControlTypeProperty)
        cache_request.AddProperty(auto.PropertyId.NameProperty)
        cache_request.AddProperty(auto.PropertyId.IsOffscreenProperty)
        cache_request.AddProperty(auto.PropertyId.NativeWindowHandleProperty)

        # FindAll with Descendants scope + cache request = single round-trip
        condition = auto.CreateTrueCondition()
        found = root.FindAll(auto.TreeScope.Descendants, condition, cacheRequest=cache_request)

        count = min(found.Length, max_elements)

        for i in range(count):
            try:
                ctrl = found.GetElement(i)

                # Use cached properties (no additional COM calls)
                rect = ctrl.CachedBoundingRectangle
                if rect is None or rect.width() <= 1 or rect.height() <= 1:
                    continue

                # Skip offscreen elements
                try:
                    if ctrl.CachedIsOffscreen:
                        continue
                except Exception:
                    pass

                control_type = ctrl.CachedControlTypeName or "Pane"
                name = ctrl.CachedName or ""
                native_hwnd = 0
                try:
                    native_hwnd = ctrl.CachedNativeWindowHandle or 0
                except Exception:
                    pass

                elements.append(UIElement(
                    left=int(rect.left), top=int(rect.top),
                    right=int(rect.right), bottom=int(rect.bottom),
                    control_type=control_type,
                    name=name,
                    hwnd=native_hwnd
                ))

            except Exception:
                continue

    except Exception:
        # Fallback to original recursive walk if FindAll fails
        try:
            elements = _walk_ui_tree_original(hwnd, max_elements)
        except Exception:
            pass

    return elements


def _walk_ui_tree_original(hwnd: int, max_depth: int = 30) -> List[UIElement]:
    """Original recursive walk as fallback."""
    elements: List[UIElement] = []
    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return elements

    def _walk(control, depth: int) -> None:
        if depth > max_depth or len(elements) >= MAX_ELEMENTS:
            return
        try:
            rect = control.BoundingRectangle
            if not control.IsOffscreen and rect and rect.width() > 1 and rect.height() > 1:
                elements.append(UIElement(
                    left=int(rect.left), top=int(rect.top),
                    right=int(rect.right), bottom=int(rect.bottom),
                    control_type=control.ControlTypeName, name=control.Name or "",
                ))
        except Exception:
            pass
        try:
            children = control.GetChildren()
        except Exception:
            children = []
        for child in children:
            if len(elements) >= MAX_ELEMENTS:
                break
            _walk(child, depth + 1)

    _walk(root, 0)
    return elements


# --------------------------------------------------------------------------
# Background worker: run the (blocking) UIA walk off the Qt UI thread
# --------------------------------------------------------------------------

class TreeWalkWorker(QtCore.QThread):
    """
    Runs walk_ui_tree_fast() on a dedicated thread with its own COM
    apartment, so the Qt UI thread is never blocked by a slow UIA scan.

    Usage: call request(hwnd) from the main thread. It is safe to call
    request() again only after the previous run has finished (isRunning()
    is False) - InspectorSession enforces this with a busy/pending flag so
    at most one scan is ever in flight.
    """
    result_ready = QtCore.pyqtSignal(int, list)  # (hwnd_scanned, elements)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hwnd: Optional[int] = None

    def request(self, hwnd: int) -> None:
        self._hwnd = hwnd
        self.start()

    def run(self) -> None:
        hwnd = self._hwnd
        elements: List[UIElement] = []
        try:
            # Initializes COM for this thread (MTA) and a thread-local UIA
            # client, as required by the uiautomation package for use
            # outside the main thread.
            with auto.UIAutomationInitializerInThread(debug=False):
                elements = walk_ui_tree_fast(hwnd)
        except Exception:
            elements = []
        self.result_ready.emit(hwnd, elements)


# --------------------------------------------------------------------------
# FAST search with preprocessed cache + SIMD fuzzy matching
# --------------------------------------------------------------------------

def build_search_cache(elements: List[UIElement]) -> SearchCache:
    """Build once per tree walk."""
    return [(el, el._search_key) for el in elements if el._search_key]


def find_search_matches_fast(cache: SearchCache, query: str, limit: int = 100) -> List[UIElement]:
    """
    Uses rapidfuzz.process.extract() - SIMD-optimized.
    """
    q = query.lower().strip()
    if not q or not cache:
        return []

    # Exact substring match first (fast path)
    exact = [el for el, key in cache if q in key]
    if exact or len(q) < 3:
        return exact

    # Fuzzy fallback using SIMD-optimized extract
    choices = [key for _, key in cache]
    results = process.extract(q, choices, scorer=fuzz.partial_ratio, limit=limit)

    matched = []
    seen = set()
    for _, score, idx in results:
        if score >= 70 and idx not in seen:
            seen.add(idx)
            matched.append(cache[idx][0])
    return matched


# --------------------------------------------------------------------------
# Session: event-driven, no polling, non-blocking scans
# --------------------------------------------------------------------------

FOREGROUND_DEBOUNCE_MS = 120


class InspectorSession(QtCore.QObject):
    elements_changed = QtCore.pyqtSignal()
    window_changed = QtCore.pyqtSignal(str)
    capture_failed = QtCore.pyqtSignal(str)
    scan_started = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.hwnd: Optional[int] = None
        self.window_title = ""
        self.elements: List[UIElement] = []
        self.search_cache: SearchCache = []
        self._own_pid = os.getpid()
        self._hooks: List[int] = []
        self._proc = _WinEventProcType(self._on_event)

        # Background UIA walk worker - keeps the UI thread from ever
        # blocking on a slow scan (this is what made window switching feel
        # slow before: the scan used to run inline on this thread).
        self._worker = TreeWalkWorker(self)
        self._worker.result_ready.connect(self._on_walk_result)
        self._walk_in_progress = False
        self._walk_pending = False

        # Debounce timer for focus changes
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(FOREGROUND_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._switch_to_foreground)

        # Throttled refresh timer
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self._do_refresh)

    def start_tracking(self) -> None:
        events = [
            (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
            (EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW),
            (EVENT_OBJECT_HIDE, EVENT_OBJECT_HIDE),
            (EVENT_OBJECT_REORDER, EVENT_OBJECT_REORDER),
            (EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE),
            (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE),
            (EVENT_OBJECT_VALUECHANGE, EVENT_OBJECT_VALUECHANGE),
        ]
        for min_event, max_event in events:
            hook = user32.SetWinEventHook(
                min_event, max_event, 0,
                self._proc, 0, 0, WINEVENT_OUTOFCONTEXT,
            )
            if hook:
                self._hooks.append(hook)

        if not self._hooks:
            poll = QtCore.QTimer(self)
            poll.setInterval(400)
            poll.timeout.connect(self._poll_foreground)
            poll.start()
            self._fallback_poll = poll

        self._switch_to_foreground()

    def shutdown(self) -> None:
        """Call on app exit so we don't leave a background COM thread dangling."""
        if self._worker.isRunning():
            self._worker.wait(2000)

    def _poll_foreground(self) -> None:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd and hwnd != self.hwnd:
            self._debounce.start()

    def _on_event(self, hook, event, hwnd, idobj, idchild, tid, ts) -> None:
        if event == EVENT_SYSTEM_FOREGROUND:
            if hwnd and hwnd != self.hwnd:
                self._debounce.start()
            return

        if self.hwnd and hwnd == self.hwnd:
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _do_refresh(self) -> None:
        self.refresh()

    def _switch_to_foreground(self) -> None:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or hwnd == self.hwnd:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == self._own_pid:
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                title = psutil.Process(pid).name()
        except Exception as exc:
            self.capture_failed.emit(str(exc))
            return
        self.hwnd = hwnd
        self.window_title = title or "(untitled window)"
        # Emit immediately so the control box updates the title instantly;
        # the (potentially slow) element scan happens in the background and
        # elements_changed fires separately once it completes.
        self.window_changed.emit(self.window_title)
        self.refresh()

    def refresh(self) -> None:
        """Kick off a background UIA scan for the current hwnd. Non-blocking."""
        if self.hwnd is None or not win32gui.IsWindow(self.hwnd):
            return
        if self._walk_in_progress:
            # A scan is already running; remember to re-scan the (possibly
            # newer) target the moment it finishes instead of stacking up
            # concurrent COM calls.
            self._walk_pending = True
            return
        self._walk_in_progress = True
        self._walk_pending = False
        self.scan_started.emit()
        self._worker.request(self.hwnd)

    def _on_walk_result(self, hwnd: int, elements: list) -> None:
        self._walk_in_progress = False
        # Discard results for a window we've since switched away from.
        if hwnd == self.hwnd:
            self.elements = elements
            self.search_cache = build_search_cache(elements)
            self.elements_changed.emit()
        if self._walk_pending:
            self._walk_pending = False
            self.refresh()


# --------------------------------------------------------------------------
# Overlay - dirty-region painting
# --------------------------------------------------------------------------

class Overlay(QtWidgets.QWidget):
    ACCENT = QtGui.QColor(0x7d, 0xd3, 0xfc)

    def __init__(self, session: InspectorSession):
        super().__init__()
        self.session = session
        self.search_query = ""
        self.search_results: List[UIElement] = []
        self._current_highlight_rects: List[QtCore.QRect] = []

        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowDoesNotAcceptFocus
            | QtCore.Qt.WindowTransparentForInput
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setGeometry(QtWidgets.QApplication.primaryScreen().virtualGeometry())

        self.session.elements_changed.connect(self._on_elements_changed)

    def _on_elements_changed(self) -> None:
        # Elements were rescanned (e.g. after a window switch or F5) - the
        # current search query may now match different on-screen positions,
        # so re-run it against the fresh element list.
        if self.search_query:
            self.search_results = find_search_matches_fast(self.session.search_cache, self.search_query)
        self._update_highlights()

    def _update_highlights(self) -> None:
        old_rects = self._current_highlight_rects.copy()
        new_rects = [el.rect for el in self.search_results] if self.search_query else []

        self._current_highlight_rects = new_rects

        for r in old_rects:
            if not any(r == nr for nr in new_rects):
                self.update(r.adjusted(-10, -10, 10, 10))

        for r in new_rects:
            if not any(r == or_ for or_ in old_rects):
                self.update(r.adjusted(-10, -10, 10, 10))

        if not old_rects and not new_rects:
            self.update()

    def set_search_query(self, query: str) -> None:
        self.search_query = query.strip()
        if self.session.search_cache:
            self.search_results = find_search_matches_fast(self.session.search_cache, self.search_query)
        else:
            self.search_results = []
        self._update_highlights()

    def _draw_highlight(self, painter: QtGui.QPainter, rect: QtCore.QRect) -> None:
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 220), 6))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRect(rect)
        painter.setPen(QtGui.QPen(self.ACCENT, 3))
        painter.drawRect(rect)

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        clip = event.rect()

        for el in self.search_results:
            if el.rect.intersects(clip):
                self._draw_highlight(painter, el.rect)


# --------------------------------------------------------------------------
# Control Box
# --------------------------------------------------------------------------

class ControlBox(QtWidgets.QWidget):
    ACCENT = "#7dd3fc"
    ACCENT_DIM = "rgba(125, 211, 252, 110)"

    def __init__(self, session: InspectorSession, overlay: Overlay):
        super().__init__()
        self.session = session
        self.overlay = overlay

        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setFont(QtGui.QFont("Segoe UI", 9))

        container = QtWidgets.QWidget(self)
        container.setObjectName("panel")
        container.setStyleSheet(
            "#panel { background: rgba(24,24,27,235); border: 1px solid rgba(255,255,255,28); border-radius: 10px; }"
        )
        shadow = QtWidgets.QGraphicsDropShadowEffect(container)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QtGui.QColor(0, 0, 0, 160))
        container.setGraphicsEffect(shadow)

        inner = QtWidgets.QVBoxLayout(container)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(8)

        self.title_label = QtWidgets.QLabel()
        self.title_label.setStyleSheet("color: rgba(244,244,245,190); font-size: 10px;")
        self.title_label.setWordWrap(True)

        self.mode_label = QtWidgets.QLabel("SEARCH")
        self.mode_label.setStyleSheet(
            f"color: {self.ACCENT}; font-size: 11px; font-weight: 600; letter-spacing: 0.3px;"
        )

        self.edit = QtWidgets.QLineEdit()
        self.edit.setMinimumHeight(34)
        self.edit.setPlaceholderText("type to filter, e.g. save, Enter to search")
        self.edit.setStyleSheet(f"""
            QLineEdit {{ background: rgba(255,255,255,10); color: #f4f4f5; border: 1px solid {self.ACCENT_DIM};
                border-radius: 7px; padding: 6px 10px; font-size: 13px; selection-background-color: {self.ACCENT}; }}
            QLineEdit:focus {{ border: 1px solid {self.ACCENT}; background: rgba(255,255,255,16); }}
        """)
        self.edit.returnPressed.connect(self._on_return_pressed)
        self.edit.installEventFilter(self)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setStyleSheet("color: rgba(244,244,245,150); font-size: 11px;")
        self.status_label.setWordWrap(True)

        self.hint_label = QtWidgets.QLabel("Enter to search  ·  F5 rescan  ·  auto-tracks the focused window  ·  Esc quit")
        self.hint_label.setStyleSheet("color: rgba(244,244,245,90); font-size: 10px;")

        for w in (self.title_label, self.mode_label, self.edit, self.status_label, self.hint_label):
            inner.addWidget(w)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.addWidget(container)

        self.resize(360, 150)
        self._anchor_bottom_right()

        self.session.window_changed.connect(self._on_window_changed)
        self.session.capture_failed.connect(self._on_capture_failed)
        self.session.elements_changed.connect(self._on_elements_changed)
        self.session.scan_started.connect(self._on_scan_started)
        self._update_status_idle()

    def _anchor_bottom_right(self, margin: int = 24) -> None:
        geo = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - self.width() - margin, geo.bottom() - self.height() - margin)

    def _on_window_changed(self, title: str) -> None:
        self.title_label.setText(f"Inspecting: {title}")

    def _on_capture_failed(self, reason: str) -> None:
        self.status_label.setText(f"Couldn't capture that window ({reason})")

    def _on_scan_started(self) -> None:
        # Gives instant feedback that the switch registered, even though
        # the element scan itself finishes a moment later in the background.
        self.status_label.setText("Scanning window…")

    def _on_elements_changed(self) -> None:
        # If there's an active query, re-show its (possibly updated) match count.
        if self.edit.text().strip():
            self._report_search_status()
        else:
            self._update_status_idle()

    def _update_status_idle(self) -> None:
        self.status_label.setText(f"{len(self.session.elements)} elements")

    def _report_search_status(self) -> None:
        total = len(self.session.elements)
        count = len(self.overlay.search_results)
        self.status_label.setText(f"{count} / {total} match" + ("" if count == 1 else "es"))

    def _on_return_pressed(self) -> None:
        text = self.edit.text()
        self.overlay.set_search_query(text)
        if text.strip():
            self._report_search_status()
        else:
            self._update_status_idle()

    def _manual_refresh(self) -> None:
        self.session.refresh()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QtCore.QEvent.KeyPress:
            key = event.key()
            if key == QtCore.Qt.Key_Escape:
                QtWidgets.QApplication.instance().quit()
                return True
            if key == QtCore.Qt.Key_F5:
                self._manual_refresh()
                return True
        return super().eventFilter(obj, event)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    _make_process_dpi_aware()
    app = QtWidgets.QApplication(sys.argv)

    session = InspectorSession()
    overlay = Overlay(session)
    control_box = ControlBox(session, overlay)

    overlay.show()
    control_box.show()
    control_box.edit.setFocus()

    session.start_tracking()
    app.aboutToQuit.connect(session.shutdown)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()