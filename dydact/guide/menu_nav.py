"""dydact/guide/menu_nav.py - highlights a menu item using Windows UI Automation (UIA)
instead of drawing an overlay box, because a native popup menu renders in a
layer above every window - including a topmost overlay reasserting itself
every frame - so no window can draw over it. The one thing that CAN render
there is the menu's own native highlight (its blue selection bar), which we
move onto the target with arrow-key presses computed from UIA's real item
tree - which also tells us which items are disabled or separators, both of
which Windows silently skips during arrow navigation, so the press count is
exact rather than guessed from a visual OCR count.

Changes vs the first version:
  * With a submenu open (File > Export > Export As...) several popup windows exist at once.
    We now look through ALL of them for the one that holds the label, not just the first.
  * Known limit: the Down presses start from wherever the highlight already is, so asking for
    the same item twice ("again") on an already-highlighted menu can overshoot. (A {Home}
    first was tried and put the highlight one item too high, so it was removed.)
  * UIA runs on a worker thread, which must initialise COM first
    (UIAutomationInitializerInThread); done inside highlight_item().

UNVERIFIED: assumes Illustrator's menu bar is a standard Win32 popup menu
(window class "#32768"), the normal case for native-menu-bar Windows apps.
If Illustrator uses a custom menu widget instead, find_menu_with() will find
nothing here and callers get a clean "no menu open" result rather than a
false positive - but this needs confirming against the real app.

Needs: pip install uiautomation
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import uiautomation as auto

from ..common import norm_label

log = logging.getLogger(__name__)

POLL_MS = 30
POLL_TIMEOUT_S = 1.5
POPUP_CLASS = "#32768"  # standard Win32 popup-menu window class


@dataclass
class MenuTarget:
    control: auto.Control
    rank: int  # how many enabled items from the top land here (1-based)


def find_open_menu(timeout_s: float = POLL_TIMEOUT_S) -> auto.Control | None:
    """The first currently open native popup menu, if any. Polls briefly since a menu
    opened by a just-sent click may still be animating in."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            for w in auto.GetRootControl().GetChildren():
                if getattr(w, "ClassName", "") == POPUP_CLASS:
                    return w
        except Exception:
            log.debug("Could not enumerate open native menus", exc_info=True)
        time.sleep(POLL_MS / 1000)
    return None


def _items(menu: auto.Control) -> list:
    """Direct MenuItem children, in on-screen (top-to-bottom) order."""
    return [c for c in menu.GetChildren() if c.ControlTypeName == "MenuItemControl"]


def _menu_label(label: str) -> str:
    # Illustrator appends shortcut text to an item's accessible name.
    label = re.split(r"[\t\x08]", label, maxsplit=1)[0].strip()
    if re.fullmatch(r"[-−]\d+", label):
        return "-" + label[1:]
    return norm_label(label)


def locate(menu: auto.Control, label: str) -> MenuTarget | None:
    """label: the visible text of the item, e.g. 'Save As...' or 'Rotate'.
    Exact match first, then a substring fallback (handles a trailing '...' or
    an '&' mnemonic the caller's label may not include)."""
    items = _items(menu)
    # "Hide Edges|Show Edges": some commands flip their wording with the app state, so a label
    # may list alternatives separated by "|". Any of them counts.
    targets = {t for a in label.split("|") if (t := _menu_label(a))}
    names = [_menu_label(it.Name) for it in items]
    match_i = next((i for i, name in enumerate(names) if name in targets), None)
    if match_i is None:
        text_targets = {t for t in targets if not t.lstrip("-").isdigit()}
        match_i = next(
            (i for i, name in enumerate(names) if any(t in name for t in text_targets)),
            None,
        )
    if match_i is None or not items[match_i].IsEnabled:
        return (
            None  # not found, or found but disabled (arrow navigation can't land there)
        )
    # Windows' arrow keys skip disabled items automatically, so rank = how many
    # ENABLED items precede-and-include the target, not its raw position.
    rank = sum(1 for it in items[: match_i + 1] if it.IsEnabled)
    return MenuTarget(items[match_i], rank)


def find_menu_with(label: str, timeout_s: float = POLL_TIMEOUT_S) -> MenuTarget | None:
    """Search every open popup menu (the newest submenu first) for an enabled item named `label`."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            popups = [
                w
                for w in auto.GetRootControl().GetChildren()
                if getattr(w, "ClassName", "") == POPUP_CLASS
            ]
            for w in reversed(popups):
                t = locate(w, label)
                if t is not None:
                    return t
        except Exception:
            log.debug("Could not locate item in native menus", exc_info=True)
        time.sleep(POLL_MS / 1000)
    return None


def highlight_item(label: str) -> list | None:
    """Finds the open popup menu holding `label`, sends the keys that move Windows' native
    highlight onto it, and returns its screen rect as [x, y, w, h] - or None if no menu is
    open or the item isn't there/enabled. Safe to call from a worker thread."""
    with auto.UIAutomationInitializerInThread():
        target = find_menu_with(label)
        if target is None:
            return None
        # A freshly opened menu has nothing highlighted, so the first Down lands on item 1:
        # `rank` Downs land on the target (this is the original, known-good behaviour).
        auto.SendKeys("{Down}" * target.rank, waitTime=0.03)
        r = target.control.BoundingRectangle
        return [r.left, r.top, r.right - r.left, r.bottom - r.top]
