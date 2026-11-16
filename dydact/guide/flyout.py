"""Resolve a detected flyout glyph to its accessible clickable item, when exposed."""

import ctypes
import logging
from ctypes import wintypes

import uiautomation as auto

from ..vision.coords import Rect

log = logging.getLogger(__name__)


def native_popup_at(cx, cy, ox, oy, screen_size):
    """Get the actual popup window even when Illustrator exposes only its icon to UIA."""
    try:
        user = ctypes.windll.user32
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        hwnd = user.WindowFromPoint(wintypes.POINT(int(cx + ox), int(cy + oy)))
        hwnd = user.GetAncestor(hwnd, 2)
        r = wintypes.RECT()
        if user.GetWindowRect(hwnd, ctypes.byref(r)):
            box = Rect(r.left - ox, r.top - oy, r.right - r.left, r.bottom - r.top)
            if (
                box.contains(cx, cy)
                and 0 < box.w < screen_size[0] * 0.6
                and 0 < box.h < screen_size[1] * 0.85
            ):
                return box
    except Exception:
        log.debug("Could not inspect native flyout window", exc_info=True)
    return None


def full_row(item, icon, popup):
    if popup is None:
        return item
    # Keep the detected item's vertical bounds: do not include adjacent menu rows.
    top = max(popup.y + 1, item.y)
    bottom = min(popup.y + popup.h - 1, item.y + item.h)
    if bottom <= top:
        return item
    return Rect(popup.x + 1, top, max(1, popup.w - 2), bottom - top)


def flyout_bounds(icon, monitor, screen_size):
    """Return monitor-relative item/popup boxes. Never expand to a whole toolbar/window."""
    ox, oy = monitor.get("left", 0), monitor.get("top", 0)
    cx, cy = icon.center
    item, popup = None, None
    try:
        with auto.UIAutomationInitializerInThread():
            control = auto.ControlFromPoint(int(cx + ox), int(cy + oy))
            for _ in range(8):
                if control is None:
                    break
                r = control.BoundingRectangle
                box = Rect(r.left - ox, r.top - oy, r.right - r.left, r.bottom - r.top)
                kind = control.ControlTypeName
                if (
                    box.contains(cx, cy)
                    and box.w >= icon.w * 0.7
                    and box.h >= icon.h * 0.7
                    and box.w <= max(400, icon.w * 12)
                    and box.h <= icon.h * 1.8
                    and kind in ("MenuItemControl", "ButtonControl", "ListItemControl")
                ):
                    item = item or box
                if (
                    box.contains(cx, cy)
                    and box.w < screen_size[0] * 0.6
                    and box.h < screen_size[1] * 0.85
                    and kind in ("MenuControl", "WindowControl", "PaneControl")
                ):
                    popup = box
                control = control.GetParentControl()
    except Exception:
        log.debug("Could not inspect accessible flyout bounds", exc_info=True)
    popup = native_popup_at(cx, cy, ox, oy, screen_size) or popup
    if item is None:
        # Conservative padding, not an assumed full-width flyout row.
        px, py = max(3, icon.w * 0.15), max(2, icon.h * 0.1)
        left, top = max(0, icon.x - px), max(0, icon.y - py)
        right, bottom = (
            min(screen_size[0], icon.x + icon.w + px),
            min(screen_size[1], icon.y + icon.h + py),
        )
        item = Rect(left, top, right - left, bottom - top)
    return full_row(item, icon, popup), popup
