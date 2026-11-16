"""The Guide: prompt text -> target -> BFS over the UI graph -> overlay, step by step.

This is the "show me where it is" engine. It was the body of main.py; it now lives here so the
course runner (dydact/course/) can drive it too.

Icon steps are found by template matching. Menu steps: the first word (File/Edit/...) is found by
OCR and boxed like an icon; every later word is inside an already-open dropdown, which nothing can
draw over, so Windows' own native highlight is moved onto it via UI Automation (menu_nav.py).
A step advances itself when the user clicks/holds inside the highlighted area, or types
next / back / again.
"""

from __future__ import annotations

import difflib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, QTimer, Signal

from ..common import norm
from ..overlay.overlay import OverlayWindow
from ..ui.frontend import PromptBox
from ..uigraph.graph import Route, Step, UIGraph
from ..vision.capture import ScreenCapture
from ..vision.coords import Rect
from ..vision.find_icon import IconFinder, Match, to_gray
from ..vision.layout import LayoutDetector
from ..vision.ocr import TextFinder, TextMatch
from .clicks import ClickWatcher
from .flyout import flyout_bounds
from .menu_nav import highlight_item

log = logging.getLogger(__name__)


HIDE_MS = (
    150  # time for Windows to repaint without our box/overlay before the screenshot
)
MAX_REPLANS = 2  # how often one target may be re-routed after an "unpredictable" step
SCAN_WORKERS = 4  # icons matched in parallel while the BFS looks at one level
RELAXED_THR = 0.75  # second-chance threshold before telling the user to switch toolbars
MENU_BAR_HEIGHT = (
    36  # px to OCR for the top-level menu word (File/Edit/Object/...); tune to your DPI
)
FLYOUT_SETTLE_MS = 120


def _search_bounds(frame, settings, opener=None):
    """Configured normalized screen region; flyout extent is in opener widths/heights."""
    h, w = frame.shape[:2]
    if opener is None:
        x, y, width, height = settings.get("region", [0, 0, 1, 1])
        if not (
            0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x and 0 < height <= 1 - y
        ):
            raise ValueError(
                "search.region must be normalized [x, y, width, height] within the screen"
            )
        bounds = (x * w, y * h, (x + width) * w, (y + height) * h)
    else:
        left, above, right, below = settings.get("flyout_extent", [2, 8, 8, 8])
        if min(left, above, right, below) < 0:
            raise ValueError("search.flyout_extent values must be nonnegative")
        bounds = (
            opener.x - left * opener.w,
            opener.y - above * opener.h,
            opener.x + (right + 1) * opener.w,
            opener.y + (below + 1) * opener.h,
        )
    x0, y0, x1, y1 = bounds
    return max(0, round(x0)), max(0, round(y0)), min(w, round(x1)), min(h, round(y1))


def _w(word: str) -> str:
    """'Show Rulers|Hide Rulers' -> 'Show Rulers / Hide Rulers' for display."""
    return word.replace("|", " / ")


NEXT_WORDS = {"next", "n", "ok", "done", "continue"}
BACK_WORDS = {"back", "prev", "previous", "b"}
AGAIN_WORDS = {"again", "retry", "redo", "r"}
VERB = {"click": "Click", "long_press": "Press and hold", "menu": "Menu:"}


@dataclass
class Entry:
    """One step on screen: a graph edge, or the target itself."""

    step: Step
    reachable: bool = True  # False: contexts.json knows no way to reveal the target
    highlight: Rect | None = None
    flyout_from: Rect | None = None
    click_bounds: Rect | None = None
    popup_bounds: Rect | None = None


class Guide(QObject):
    """Prompt text -> target -> BFS over the UI graph -> overlay, step by step."""

    _done = Signal(object, object, float)  # Entry, Match | None | Exception, seconds
    _ready = Signal(object)  # None, or the exception raised while loading refs
    _expanded = Signal(
        int, int, object, object, float
    )  # generation, cursor, Route | Exception, {node: Match}, seconds
    route_finished = Signal(str)  # node id of a target whose route was fully shown
    _menu_found = Signal(
        object, str, object, float
    )  # Entry, word, TextMatch | None | Exception, seconds

    def __init__(
        self, overlay: OverlayWindow, box: PromptBox, cap: ScreenCapture, graph: UIGraph
    ):
        super().__init__()
        self.overlay, self.box, self.cap, self.graph = overlay, box, cap, graph
        self.layout_detector = LayoutDetector(graph.app, cap.monitor)
        self.hide_ms = HIDE_MS  # 0 once our windows are excluded from screenshots
        self.gen = 0  # bumped on every reset: late results of an old search are dropped
        # One worker: loads the refs first (slow), then runs routing/searches in order.
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._scan = ThreadPoolExecutor(
            max_workers=SCAN_WORKERS
        )  # parallel icon matching inside a route
        self._finder = self._pool.submit(IconFinder)
        # OCR loads on its own thread so it no longer blocks the window from appearing.
        self._loader = ThreadPoolExecutor(max_workers=1)
        self._text_future = self._loader.submit(TextFinder)
        self._text_future.add_done_callback(self._text_loaded)
        self.clicks = ClickWatcher(origin=(cap.monitor["left"], cap.monitor["top"]))
        self._busy = False
        self._reset()
        self._done.connect(self._show_result)
        self._ready.connect(self._on_ready)
        self._expanded.connect(self._on_expanded)
        self._menu_found.connect(self._show_menu_result)
        self.clicks.clicked_in.connect(self._on_box_clicked)
        box.set_status("Loading icon references...")
        self._finder.add_done_callback(lambda f: self._ready.emit(f.exception()))
        box.submitted.connect(self.search)
        box.esc_pressed.connect(self.clear)
        box.settings_changed.connect(self.apply_settings)

    # ---- state ---------------------------------------------------------------
    def _reset(self):
        self.gen += 1
        self.target: str | None = None  # node id being guided to; None = idle
        self.entries: list[
            Entry
        ] = []  # every step expanded so far (back/again use this)
        self.cursor = -1  # entry being shown
        self.finished = False  # the route reached the target itself
        self.selected = False  # the user passed a "select something" step
        self.replans = 0
        self._pre: dict = {}  # matches the BFS already found on the latest screenshot
        self._menu_idx = 0  # which word of the current menu step's path we're on
        self._pending: str | None = (
            None  # text typed while busy; runs when the work is done
        )

    def _idle(self):
        """Work finished: unlock the input and start whatever the user typed meanwhile."""
        self._busy = False
        self.box.set_busy(False)
        if self._pending:
            text, self._pending = self._pending, None
            QTimer.singleShot(0, lambda: self.search(text))

    def _stale(self, e: Entry) -> bool:
        """True when a worker result belongs to a search that was cleared/replaced meanwhile."""
        return self.target is None or not any(x is e for x in self.entries)

    @property
    def _text_finder(self):
        f = self._text_future
        return f.result() if f.done() and f.exception() is None else None

    def _text_error(self):
        f = self._text_future
        return (
            "still loading, try again in a moment"
            if not f.done()
            else (f.exception() or "unavailable")
        )

    def _text_loaded(self, f):  # runs on the loader thread: print only
        if f.exception() is not None:
            print(
                f"[dydact] menu-bar highlighting disabled: {f.exception()}", flush=True
            )

    def apply_settings(self, fast: bool, hide_ms: int):
        self.hide_ms = 0 if fast else hide_ms

    def _on_ready(self, err):
        if err is not None:
            self.box.set_status(f"Could not load icon references: {err}", "error")
            return
        finder = self._finder.result()
        missing = [
            n
            for n in self.graph.icon_names()
            if n not in finder.refs and not self.graph.nodes[n].uia_selector
        ]
        if missing:
            print(
                f"[dydact] {len(missing)} icons in contexts.json have no matching templates yet: "
                f"{', '.join(missing)}",
                flush=True,
            )
        if not self._busy:  # don't overwrite a search that was queued while loading
            tail = (
                f" ({len(missing)} icons have no refs yet - see console)"
                if missing
                else ""
            )
            self.box.set_status(
                f"Ready - type a target name ({len(self.graph.nodes)} in contexts.json).{tail}"
            )

    def clear(self):
        self._reset()
        self.clicks.disarm()
        self.overlay.clear()
        self.box.setWindowOpacity(1.0)
        self._busy = False
        self.box.set_busy(False)
        self.box.set_status("")

    # ---- input -------------------------------------------------------------
    def search(self, text: str):
        if self._busy:
            self._pending = text  # don't drop it: run it when the work is done
            self.box.set_status("Still working - your input is queued.")
            return
        t = text.strip()
        low = t.lower()
        if self.target is not None:  # walking through a route: navigation words
            if not t or low in NEXT_WORDS:
                return self._go(self.cursor + 1)
            if low in BACK_WORDS:
                return self._go(self.cursor - 1)
            if low in AGAIN_WORDS:
                if self.cursor == -1:
                    return self._expand(0)
                return self._go(self.cursor)
        if not t:
            return
        node = self.graph.resolve(t)
        if node is None:
            close = difflib.get_close_matches(
                norm(t), list(self.graph.by_norm), n=3, cutoff=0.6
            )
            hint = (
                f" Did you mean: {', '.join(self.graph.by_norm[c] for c in close)}?"
                if close
                else ""
            )
            self.box.set_status(f"'{t}' is not in contexts.json.{hint}", "error")
            self.box.input.selectAll()
            return
        self._reset()
        self.target = node
        self._go(0)

    # ---- click-to-advance ----------------------------------------------------
    def _on_box_clicked(self, held: bool):
        if self._busy or self.target is None:
            return
        if 0 <= self.cursor < len(self.entries):
            e = self.entries[self.cursor]
            node = self.graph.nodes[e.step.node]
            if e.step.action == "long_press" and not held:
                # a quick click only selects the tool; the flyout needs the button held down
                self.box.set_status(
                    f"{self._line(e)}  - that was a quick click; keep the mouse button "
                    f"held down a moment to open the flyout.{self._nav_hint()}"
                )
                return
            if node.kind == "menu" and self._menu_idx < len(node.path) - 1:
                self._menu_idx += 1
                return self._locate_menu_word(e)
        self._go(self.cursor + 1)  # same path "next" already takes

    # ---- walking the steps -------------------------------------------------
    def _go(self, i: int):
        if self.target is None:
            return
        if i < 0:
            self.box.set_status("Already at the first step.", "error")
            self.box.input.selectAll()
            return
        if (
            0 <= self.cursor < len(self.entries)
            and i > self.cursor
            and self.entries[self.cursor].step.sets_selection
        ):
            self.selected = True  # the user got past "select something"
        if i < len(self.entries):
            if 0 <= self.cursor == i - 1:
                previous = self.entries[self.cursor]
                if previous.step.action == "long_press":
                    self.entries[i].flyout_from = previous.highlight
            self.cursor = i
            return self._show_entry(i)
        if self.finished:  # everything shown
            self.clicks.disarm()
            self.overlay.clear()
            done = self.target
            self.box.set_status(
                f"Done - {len(self.entries)} step(s) shown for {done}.", "ok"
            )
            self.target = None
            self.box.input.selectAll()
            self.route_finished.emit(done)
            return
        self._expand(i)

    # ---- hide our own windows (unless they are excluded from capture), then capture ---
    def _after_hide(self, fn, *, settle_ms=0):
        gen = self.gen
        self.clicks.disarm()
        self.overlay.clear()
        delay = max(self.hide_ms, settle_ms)
        if not delay:
            fn()
            return
        if self.hide_ms:
            self.box.setWindowOpacity(0.0)

        def run():
            if gen != self.gen:  # cleared / replaced while we waited
                return
            fn()

        QTimer.singleShot(delay, run)

    # ---- BFS: expand the target into steps (fresh screenshot) ---------------
    def _expand(self, i: int):
        self._busy = True
        self.box.set_busy(True)
        self.box.set_status("Looking at the screen...")
        gen = self.gen
        self._after_hide(lambda: self._capture_for_route(i, gen))

    def _capture_for_route(self, i: int, gen: int):
        capture = self._grab_screen()
        if capture is None:
            return
        frame, visible = capture
        self.box.setWindowOpacity(1.0)
        self._pool.submit(
            self._route_worker, frame, visible, i, self.target, self.selected, gen
        )

    def _route_worker(
        self, frame, visible, i: int, target: str, selected: bool, gen: int
    ):  # worker thread
        t0 = time.perf_counter()
        seen: dict = {}
        try:
            finder = self._finder.result()
            node = self.graph.nodes[target]
            if node.locatable and not node.uia_selector and target not in finder.refs:
                raise ValueError(f"{target} has no matching images in data/refs.")
            if node.uia_selector:
                hit, panel_visible = self._uia_control(target, visible, refresh=True)
                if hit is not None or target not in finder.refs:
                    if hit is None and panel_visible:
                        raise ValueError(self._control_hint(node))
                    seen[target] = hit

                    def located(name):
                        if name not in seen:
                            seen[name] = self._uia_control(name, visible, refresh=True)[
                                0
                            ]
                        return seen[name] is not None

                    route = self.graph.route(
                        target,
                        located,
                        assume_selected=selected,
                    )
                    self._expanded.emit(gen, i, route, seen, time.perf_counter() - t0)
                    return
            gray = to_gray(frame)  # convert once per screenshot
            layout_started = time.perf_counter()
            layout = self.layout_detector.detect(visible, frame=frame)
            layout_ms = (time.perf_counter() - layout_started) * 1000
            finder.search_regions = layout.regions
            finder.prepare_frame(gray, visible=visible)
            warm_started = time.perf_counter()
            if not finder.scale_confirmed:
                finder.warm_scale(gray, visible=layout.mask)
            log.info(
                "Search preparation target=%s layout_ms=%.1f warm_ms=%.1f confirmed=%s",
                target,
                layout_ms,
                (time.perf_counter() - warm_started) * 1000,
                finder.scale_confirmed,
            )
            bfs_started = time.perf_counter()
            res = self._route(
                finder,
                gray,
                target,
                selected,
                seen,
                visible=layout.mask,
                app_visible=visible,
            )
            log.info(
                "BFS target=%s relaxed=False elapsed_ms=%.1f scale=%g confirmed=%s",
                target,
                (time.perf_counter() - bfs_started) * 1000,
                finder.scale_hint,
                finder.scale_confirmed,
            )
            if not res.reachable or any(s.replan for s in res.steps):
                # The graph is about to say "switch toolbar" / "can't reach it". That is only right
                # if the icons really are absent, so look once more: whole scale sweep, lower
                # threshold. (Screens where the first pass missed an icon end
                # up here; the box label shows the score so you can see how sure the match is.)
                seen = {}
                bfs_started = time.perf_counter()
                res = self._route(
                    finder,
                    gray,
                    target,
                    selected,
                    seen,
                    relaxed=True,
                    visible=layout.mask,
                    app_visible=visible,
                )
                log.info(
                    "BFS target=%s relaxed=True elapsed_ms=%.1f scale=%g confirmed=%s",
                    target,
                    (time.perf_counter() - bfs_started) * 1000,
                    finder.scale_hint,
                    finder.scale_confirmed,
                )
        except (
            Exception
        ) as e:  # e.g. an icon in contexts.json with no refs/<icon>/ folder
            log.debug("_route_worker failed; using existing fallback", exc_info=True)
            res = e
        self._expanded.emit(gen, i, res, seen, time.perf_counter() - t0)

    def _uia_control(self, name, visible, *, refresh=False):
        selector = self.graph.nodes[name].uia_selector
        scope = self.graph.control_scopes.get(name)
        if not selector or (scope and not refresh):
            return None, False
        box, panel_visible = self.layout_detector.visible_control(
            selector,
            visible,
            refresh=refresh,
            scope=scope,
        )
        hit = Match(name, 1.0, box, 1.0, "uia") if box is not None else None
        return hit, panel_visible

    def _control_hint(self, node):
        action = (
            "Scroll the panel or select artwork of the appropriate type, then try again."
            if node.needs_selection
            else "Scroll the panel or deselect the artwork, then try again."
        )
        return f"{node.id} is unavailable in the current Properties panel. {action}"

    def _route(
        self,
        finder,
        gray,
        target: str,
        selected: bool,
        seen: dict,
        relaxed: bool = False,
        *,
        visible=None,
        app_visible,
    ) -> Route:
        def match(name: str):
            hit, _ = self._uia_control(name, app_visible)
            if hit is not None:
                return hit
            if name in finder.refs:
                # Try the wider scale range for the requested icon before choosing a fallback route.
                if relaxed and name == target:
                    hit = finder.find(
                        gray,
                        name,
                        visible=visible,
                        extended_scales=True,
                        full_sweep=True,
                    )
                    if hit is not None:
                        return hit
                return (
                    finder.find(
                        gray, name, visible=visible, thr=RELAXED_THR, full_sweep=True
                    )
                    if relaxed
                    else finder.find(gray, name, visible=visible)
                )
            return None

        def look(name: str):
            if name not in seen:
                seen[name] = match(name)
            return seen[name]

        def prefetch(names: list):  # the BFS is about to ask about all of these
            todo = [n for n in dict.fromkeys(names) if n not in seen]
            if len(todo) > 1:
                seen.update(zip(todo, self._scan.map(match, todo)))

        res = self.graph.route(
            target,
            lambda n: look(n) is not None,
            assume_selected=selected,
            prefetch=prefetch,
        )
        first = res.steps[0].node
        if self.graph.nodes[first].locatable:
            look(first)  # e.g. the optional "select something" step
        return res

    def _on_expanded(self, gen: int, i: int, route, seen: dict, secs: float):
        if gen != self.gen:  # Esc / a new search happened while the worker ran
            return
        self._idle()
        if self.target is None:
            return
        if isinstance(route, Exception):
            self.box.set_status(f"{route}", "error")
            return
        self.finished = route.steps[-1].final
        if not self.finished:  # a step whose result can't be predicted (toolbar switch): route again after it
            self.replans += 1
            if self.replans > MAX_REPLANS:
                self.box.set_status(
                    f"Still can't find {self.target} after switching toolbars. Check that it "
                    f"exists in your workspace.",
                    "error",
                )
                return
        for s in route.steps:
            self.entries.append(Entry(s, reachable=route.reachable or not s.final))
        self._pre = seen
        self._go(i)

    # ---- show one step -----------------------------------------------------
    def _show_entry(self, i: int):
        e = self.entries[i]
        node = self.graph.nodes[e.step.node]
        if node.kind == "menu":
            self._pre = {}
            self._menu_idx = 0
            return self._locate_menu_word(e)
        if node.id in self._pre:  # BFS already matched it on this very screenshot
            m = self._pre[node.id]
            self._pre = {}
            return self._show_result(e, m, 0.0)
        self._pre = {}
        self._locate(e)

    def _line(self, e: Entry) -> str:
        s, node = e.step, self.graph.nodes[e.step.node]
        why = node.does if s.final else s.why
        opt = " (optional)" if s.optional else ""
        return f"Step {self.cursor + 1}{opt}: {VERB.get(s.action, 'Click')} {node.label()}. {why}".strip()

    def _locate(self, e: Entry):
        self._busy = True
        self.box.set_busy(True)
        self.box.set_status(f"Looking for {e.step.node}...")
        self._after_hide(
            lambda: self._capture(e), settle_ms=FLYOUT_SETTLE_MS if e.flyout_from else 0
        )

    def _capture(self, e: Entry):
        capture = self._grab_screen()
        if capture is None:
            return
        frame, visible = capture
        self.box.setWindowOpacity(1.0)
        self._pool.submit(self._find, frame, visible, e)

    def _find(self, frame, visible, e: Entry):  # worker thread
        t0 = time.perf_counter()
        try:
            finder = self._finder.result()
            node = self.graph.nodes[e.step.node]
            if node.uia_selector:
                hit, panel_visible = self._uia_control(node.id, visible, refresh=True)
                if hit is not None or node.id not in finder.refs:
                    if hit is None and panel_visible:
                        hit = ValueError(self._control_hint(node))
                    self._done.emit(e, hit, time.perf_counter() - t0)
                    return
            gray = to_gray(frame)
            finder.prepare_frame(gray, visible=visible)
            if e.flyout_from is None:
                layout = self.layout_detector.detect(visible, frame=frame)
                finder.search_regions = layout.regions
                visible = layout.mask
            else:
                finder.search_regions = None  # Open flyouts override canvas exclusions.
            x0, y0, x1, y1 = (
                _search_bounds(gray, self.graph.search, e.flyout_from)
                if e.flyout_from
                else (0, 0, gray.shape[1], gray.shape[0])
            )
            result = finder.find(
                gray[y0:y1, x0:x1],
                e.step.node,
                visible=visible[y0:y1, x0:x1],
                flyout=e.flyout_from is not None,
                extended_scales=True,
            )
            if result is not None:
                result = replace(
                    result,
                    box=[result.box[0] + x0, result.box[1] + y0, *result.box[2:]],
                )
                if e.flyout_from is not None:
                    e.click_bounds, e.popup_bounds = flyout_bounds(
                        Rect(*result.box),
                        self.cap.monitor,
                        (frame.shape[1], frame.shape[0]),
                    )
        except Exception as ex:
            log.debug("_find failed; using existing fallback", exc_info=True)
            result = ex
        self._done.emit(e, result, time.perf_counter() - t0)

    def _nav_hint(self) -> str:
        if self.target is None:
            return ""
        last = self.cursor + 1 >= len(self.entries) and self.finished
        return (
            "  [next to finish / back / again]" if last else "  [next / back / again]"
        )

    def _show_result(self, e: Entry, m, secs: float):
        if self._stale(e):
            return
        self._idle()
        self.box.input.selectAll()
        line, icon = self._line(e), e.step.node
        if isinstance(m, Exception):
            self.clicks.disarm()
            self.box.set_status(f"{line}  - {m}{self._nav_hint()}", "error")
        elif m is None:
            self.clicks.disarm()
            why = (
                "contexts.json knows no way to reveal it"
                if not e.reachable
                else "it may be in a flyout or a closed panel"
            )
            self.box.set_status(
                f"{line}  - {icon} is not on screen ({secs:.1f} s); {why}.{self._nav_hint()}",
                "error",
            )
        else:
            x, y, w, h = m.box
            e.highlight = e.click_bounds or Rect(x, y, w, h)
            self.clicks.arm(
                e.highlight,
                hold=e.step.action == "long_press",
                release=e.flyout_from is not None and e.step.action != "long_press",
            )
            is_flyout = e.flyout_from is not None
            if is_flyout:
                self.overlay.clear()
                self.overlay.show_box(e.highlight, flyout=e.popup_bounds or e.highlight)
            else:
                score = "" if m.ref == "uia" else f"  {m.score:.2f}"
                self.overlay.show_box(e.highlight, f"{self.cursor + 1}. {icon}{score}")
            cx, cy = m.center
            took = f", {secs:.1f} s" if secs else ""
            instruction = (
                " Release over the highlighted row to select it (or click if already released)."
                if is_flyout
                else ""
            )
            self.box.set_status(
                f"{line}  [{icon} at ({cx}, {cy}){took}]{instruction}{self._nav_hint()}",
                "ok",
            )

    # ---- menu steps: word 0 (top bar) via OCR+box; later words (inside an
    # already-open dropdown) via UIA native highlight - nothing we draw can
    # render above an open popup menu, but Windows' own highlight can -------
    def _locate_menu_word(self, e: Entry):
        node = self.graph.nodes[e.step.node]
        word = node.path[self._menu_idx]
        self._busy = True
        self.box.set_busy(True)
        self.box.set_status(f"Looking for '{_w(word)}'...")
        self.clicks.disarm()
        self.overlay.clear()
        if self._menu_idx == 0:
            if self._text_finder is None:
                self._idle()
                self.box.set_status(
                    f"Menu highlighting unavailable ({self._text_error()}). "
                    f"{VERB['menu']} {node.label()}{self._nav_hint()}",
                    "error",
                )
                return
            self._after_hide(lambda: self._capture_menu(e, word))
        else:
            self._pool.submit(self._find_menu_word_uia, e, word)

    def _capture_menu(self, e: Entry, word: str):
        capture = self._grab_screen()
        if capture is None:
            return
        frame, visible = capture
        self.box.setWindowOpacity(1.0)
        top = int(visible.any(axis=1).argmax())
        region = (
            0,
            top,
            frame.shape[1],
            MENU_BAR_HEIGHT,
        )  # top menu bar only (word index 0)
        self._pool.submit(self._find_menu_word, frame, e, word, region)

    def _grab_screen(self):
        try:
            return self.cap.grab_app(
                self.layout_detector.profile.get("processes", ["illustrator.exe"])
            )
        except Exception as e:
            log.debug("_grab_screen failed; using existing fallback", exc_info=True)
            self.box.setWindowOpacity(1.0)
            self._idle()
            self.box.set_status(f"Screen capture failed: {e}", "error")
            self.box.input.selectAll()
            return None

    def _find_menu_word(self, frame, e: Entry, word: str, region):  # worker thread
        t0 = time.perf_counter()
        try:
            result = self._text_finder.find(frame, word, region=region)
        except Exception as ex:
            log.debug("_find_menu_word failed; using existing fallback", exc_info=True)
            result = ex
        self._menu_found.emit(e, word, result, time.perf_counter() - t0)

    def _find_menu_word_uia(self, e: Entry, word: str):  # worker thread
        t0 = time.perf_counter()
        try:
            box = highlight_item(
                word
            )  # finds the open popup, moves the native highlight, returns its rect
            result = TextMatch(word, 1.0, box) if box else None
        except Exception as ex:
            log.debug(
                "_find_menu_word_uia failed; using existing fallback", exc_info=True
            )
            result = ex
        self._menu_found.emit(e, word, result, time.perf_counter() - t0)

    def _show_menu_result(self, e: Entry, word: str, m, secs: float):
        if self._stale(e):
            return
        self._idle()
        self.box.input.selectAll()
        node = self.graph.nodes[e.step.node]
        last = self._menu_idx == len(node.path) - 1
        prefix = f"Step {self.cursor + 1}: "
        if isinstance(m, Exception):
            self.clicks.disarm()
            self.box.set_status(
                f"{prefix}error locating '{_w(word)}' - {m}{self._nav_hint()}", "error"
            )
            return
        if m is None:
            self.clicks.disarm()
            why = (
                "it may need a wider search region, or the menu bar isn't where expected"
                if self._menu_idx == 0
                else "no menu is currently open, or the item isn't visible/enabled in it"
            )
            self.box.set_status(
                f"{prefix}couldn't find '{_w(word)}' ({secs:.1f}s) - {why}.{self._nav_hint()}",
                "error",
            )
            return
        x, y, w, h = m.box
        self.clicks.arm(Rect(x, y, w, h))
        if self._menu_idx == 0:  # nothing covers the menu bar: draw our own box
            self.overlay.show_box(
                Rect(x, y, w, h), f"{self.cursor + 1}. {word}  {m.score:.2f}"
            )
            hl = ""
        else:  # inside an open dropdown: Windows' own highlight
            self.overlay.clear()  # is already on it (moved there by highlight_item)
            hl = " (highlighted on screen)"
        why = f"  {node.does}" if last and node.does else ""
        self.box.set_status(
            f"{prefix}Click '{_w(word)}'{hl}{why}{self._nav_hint()}", "ok"
        )
