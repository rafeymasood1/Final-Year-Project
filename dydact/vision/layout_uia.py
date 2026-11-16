"""Bounded UIA probe run in a disposable process so an unresponsive app cannot hang search."""

import ctypes
import json
import logging
import os
import sys
from collections import deque
from ctypes import wintypes
from re import IGNORECASE, fullmatch

import uiautomation as auto
from comtypes.client import CreateObject

log = logging.getLogger(__name__)


def process_name(pid):
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel.CloseHandle(handle)


def app_windows(processes):
    """Filter cheap native window handles BEFORE contacting any accessibility provider."""
    names = {p.casefold() for p in processes}
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handles, cache = [], {}

    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in cache:
            cache[pid.value] = process_name(pid.value)
        if cache[pid.value] in names:
            handles.append(hwnd)
        return True

    user.EnumWindows(visit, 0)
    return handles


def native_children(handles, monitor):
    """Inventory real child-window bounds; class selectors must be verified per app."""
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    nodes = []

    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return True
        name = ctypes.create_unicode_buffer(512)
        user.GetClassNameW(hwnd, name, len(name))
        nodes.append(
            {
                "name": "",
                "control_type": "NativeWindow",
                "class_name": name.value,
                "automation_id": "",
                "source": "win32",
                "box": [
                    rect.left - monitor.get("left", 0),
                    rect.top - monitor.get("top", 0),
                    rect.right - rect.left,
                    rect.bottom - rect.top,
                ],
                "floating": False,
            }
        )
        return len(nodes) < 1000

    for hwnd in handles:
        user.EnumChildWindows(hwnd, visit, 0)
    return nodes


def _matches(node, selector):
    return all(
        fullmatch(value, node.get(key, ""), IGNORECASE)
        for key, value in selector.items()
    )


def _control_node(control, monitor, *, floating=False):
    if control.IsOffscreen:
        return None
    r = control.BoundingRectangle
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return {
        "name": control.Name or "",
        "control_type": control.ControlTypeName,
        "class_name": control.ClassName or "",
        "automation_id": control.AutomationId or "",
        "box": [
            r.left - monitor.get("left", 0),
            r.top - monitor.get("top", 0),
            r.right - r.left,
            r.bottom - r.top,
        ],
        "floating": floating,
    }


def _scoped_probe(roots, monitor, selector, scope):
    # Find the panel by its exact provider name, then filter by control type
    # within that panel before reading names and bounds across the UIA boundary.
    from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation

    client = CreateObject(CUIAutomation, interface=IUIAutomation)
    descendants = 4  # TreeScope_Descendants
    pane_name = client.CreatePropertyCondition(
        auto.PropertyId.NameProperty, scope["name"]
    )
    control_type = next(
        value
        for value, name in auto.ControlTypeNames.items()
        if name == selector["control_type"]
    )
    kind = client.CreatePropertyCondition(
        auto.PropertyId.ControlTypeProperty, control_type
    )
    for _, root in roots:
        pane = auto.Control.CreateControlFromElement(
            root.Element.FindFirst(descendants, pane_name)
        )
        if pane is None:
            continue
        scope_node = _control_node(pane, monitor)
        if scope_node is None or not _matches(scope_node, scope):
            continue
        matches = pane.Element.FindAll(descendants, kind)
        for i in range(matches.Length):
            control = auto.Control.CreateControlFromElement(matches.GetElement(i))
            if not fullmatch(selector["name"], control.Name or "", IGNORECASE):
                continue
            node = _control_node(control, monitor)
            if node and _matches(node, selector) and control.IsEnabled:
                return [scope_node, node]
        return [scope_node]
    return []


def probe(processes, monitor, selector=None, scope=None):
    handles = app_windows(processes)
    native = [] if selector else native_children(handles, monitor)
    nodes, roots = [], []
    with auto.UIAutomationInitializerInThread():
        for hwnd in handles:
            control = auto.ControlFromHandle(hwnd)
            if control is not None and not control.IsOffscreen:
                r = control.BoundingRectangle
                roots.append(
                    (max(0, r.right - r.left) * max(0, r.bottom - r.top), control)
                )
        roots.sort(key=lambda pair: pair[0], reverse=True)
        if selector and scope:
            return _scoped_probe(roots, monitor, selector, scope)
        pending = deque((control, 0, i > 0) for i, (_, control) in enumerate(roots))
        while pending and len(nodes) < 600:
            control, depth, floating = pending.popleft()
            try:
                node = _control_node(control, monitor, floating=floating and depth == 0)
                if node is None:
                    continue
                if selector and _matches(node, selector) and control.IsEnabled:
                    return [node]
                nodes.append(node)
                if depth < 12:
                    pending.extend(
                        (child, depth + 1, False) for child in control.GetChildren()
                    )
            except Exception:
                log.debug("probe failed; using existing fallback", exc_info=True)
                continue
    return [] if selector else nodes + native


if __name__ == "__main__":
    args = json.load(sys.stdin)
    print(
        json.dumps(
            probe(
                args["processes"],
                args["monitor"],
                args.get("selector"),
                args.get("scope"),
            )
        )
    )
