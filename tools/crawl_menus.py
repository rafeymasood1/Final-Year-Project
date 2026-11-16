"""Read Illustrator's menus or Properties panel through Windows UI Automation.

    1. Start Illustrator on Windows, open any document, leave it in front.
    2. python tools/crawl_menus.py
    3. python tools/build_contexts.py tools/menu_tree.crawled.txt          (add --prune to drop
       commands your Illustrator does not have)

It opens each top-level menu and reads the native Windows popup tree, including submenus
such as Window > Workspace. Leave Illustrator in front and avoid using the keyboard while
the crawl runs. It only opens and dismisses menus.

    python tools/crawl_menus.py --probe     if nothing is found: print what UI Automation can see
    python tools/crawl_menus.py --only File,Edit   crawl only some top menus (quick test)

For Properties, open Window > Properties and set the tool or selection you want to capture.
    python tools/crawl_menus.py --properties --context no_selection
    python tools/build_contexts.py --properties tools/properties.crawled.json
Repeat for other states, choosing a context name for each one. This mode only reads the panel.

Menu crawling assumes native Win32 popup menus. Use --probe to inspect the window
if menu crawling fails, or export the shortcuts from Edit > Keyboard Shortcuts.

Needs: pip install uiautomation
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto

log = logging.getLogger(__name__)


OUT = Path(__file__).with_name("menu_tree.crawled.txt")
PROPERTIES_OUT = Path(__file__).with_name("properties.crawled.json")
PROPERTIES_PANE = "AdobePropertiesPanel"
POPUP = "#32768"  # standard Win32 popup-menu window class
MN_GETHMENU = 0x01E1
MF_BYPOSITION = 0x0400
SMTO_ABORTIFHUNG = 0x0002
MENU_KEYS = {
    "File": "f",
    "Edit": "e",
    "Object": "o",
    "Type": "t",
    "Select": "s",
    "Effect": "c",
    "View": "v",
    "Window": "w",
    "Help": "h",
}
SKIPPED_LISTS = {"open recent files", "open recent", "recent fonts", "font"}
DOCUMENT_TITLE = re.compile(r" @ \d+(?:[.,]\d+)?\s?%")
EFFECT_HEADINGS = {"Illustrator Effects", "Photoshop Effects"}


def popups(process_id: int) -> list:
    try:
        return [
            w
            for w in auto.GetRootControl().GetChildren()
            if w.ClassName == POPUP and w.ProcessId == process_id
        ]
    except Exception:
        log.debug("popups failed; using existing fallback", exc_info=True)
        return []


def menu_items(container) -> list:
    return [
        c
        for c in container.GetChildren()
        if c.ControlTypeName == "MenuItemControl" and (c.Name or "").strip()
    ]


def clean(name: str) -> str:
    return " ".join(re.sub(r"&(\S)", r"\1", name).split())


def wait_for_popup(process_id: int, timeout: float = 1.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        menus = popups(process_id)
        if menus:
            return menus[0]
        time.sleep(0.05)
    return None


class NativeMenus:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32")
        self.user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        self.user32.GetMenuItemCount.argtypes = [wintypes.HMENU]
        self.user32.GetSubMenu.argtypes = [wintypes.HMENU, ctypes.c_int]
        self.user32.GetSubMenu.restype = wintypes.HMENU
        self.user32.GetMenuStringW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.UINT,
        ]

    def popup_handle(self, hwnd: int) -> int:
        result = ctypes.c_size_t()
        success = self.user32.SendMessageTimeoutW(
            hwnd, MN_GETHMENU, 0, 0, SMTO_ABORTIFHUNG, 1000, ctypes.byref(result)
        )
        return result.value if success else 0

    def crawl(self, menu: int, path: tuple[str, ...]) -> list[str]:
        lines = []
        indent = "  " * len(path)
        for index in range(self.user32.GetMenuItemCount(menu)):
            text = ctypes.create_unicode_buffer(4096)
            self.user32.GetMenuStringW(menu, index, text, len(text), MF_BYPOSITION)
            parts = re.split(r"[\t\x08]", text.value, maxsplit=1)
            label = clean(parts[0])
            if not label:
                continue
            if path == ("Window",) and DOCUMENT_TITLE.search(label):
                continue
            if path == ("Effect",) and label in EFFECT_HEADINGS:
                continue
            sub = self.user32.GetSubMenu(menu, index)
            if sub:
                child_path = (*path, label)
                if label.casefold() in SKIPPED_LISTS:
                    lines.append(
                        indent + f"# skipped dynamic list: {' > '.join(child_path)}"
                    )
                    continue
                children = self.crawl(sub, child_path)
                if not any(not child.lstrip().startswith("#") for child in children):
                    lines.append(
                        indent + f"# no commands in submenu: {' > '.join(child_path)}"
                    )
                    lines.extend(children)
                    continue
                lines.append(indent + label)
                lines.extend(children)
            else:
                shortcut = parts[1].strip() if len(parts) > 1 else ""
                lines.append(indent + label + (f" | {shortcut}" if shortcut else ""))
        return lines


def crawl_menus(win, only: set[str] | None) -> tuple[list[str], list[str]]:
    # Illustrator's main menu bar can be missing from UIA even when Alt+letter
    # opens its native popup. Prefer visible top labels when UIA exposes them.
    labels = {
        clean(item.Name): item
        for child in win.GetChildren()
        if child.ControlTypeName == "MenuBarControl"
        for item in menu_items(child)
    }
    menus = [
        (name, key)
        for name, key in MENU_KEYS.items()
        if not only or name.casefold() in only
    ]
    native = NativeMenus()
    pid = win.ProcessId
    lines = [
        "# Crawled from the live Illustrator menus. Feed to tools/build_contexts.py",
        "",
    ]
    missing = []
    for name, key in menus:
        if auto.GetForegroundWindow() != win.NativeWindowHandle:
            raise RuntimeError(
                "Leave Illustrator in the foreground while crawling menus."
            )
        if popups(pid):
            auto.SendKeys("{Esc}", waitTime=0.12)
        if popups(pid):
            raise RuntimeError("Close all Illustrator popups before running the crawl.")
        print(f"crawling {name} ...", flush=True)
        try:
            if name in labels:
                labels[name].Click(simulateMove=False)
            else:
                auto.SendKeys("{Alt}" + key, charMode=False, waitTime=0.12)
            popup = wait_for_popup(pid)
            if popup is None:
                missing.append(name)
                continue
            menu = native.popup_handle(popup.NativeWindowHandle)
            entries = native.crawl(menu, (name,)) if menu else []
            if not any(not line.lstrip().startswith("#") for line in entries):
                missing.append(name)
                continue
            lines.extend([name, *entries, ""])
        finally:
            if popups(pid):
                auto.SendKeys("{Esc}", waitTime=0.12)
    return lines, missing


def find_illustrator():
    windows = [
        w
        for w in auto.GetRootControl().GetChildren()
        if w.ControlTypeName == "WindowControl"
    ]
    return next(
        (w for w in windows if w.ClassName.casefold() == "illustrator"),
        next((w for w in windows if "illustrator" in (w.Name or "").casefold()), None),
    )


def crawl_properties(win) -> list[dict]:
    """Return the UIA subtree of the visible Properties pane in tree order."""
    panel = next(
        (
            control
            for control, _ in auto.WalkControl(win, maxDepth=12)
            if control.ControlTypeName == "PaneControl"
            and PROPERTIES_PANE in (control.Name, control.AutomationId)
            and not control.IsOffscreen
        ),
        None,
    )
    if panel is None:
        return []

    controls = []
    for control, depth in auto.WalkControl(panel, maxDepth=12):
        kind = control.ControlTypeName
        name = control.Name or ""
        entry = {
            "depth": depth,
            "name": name,
            "control_type": kind,
            "enabled": bool(control.IsEnabled),
            "offscreen": bool(control.IsOffscreen),
        }
        for key, value in (
            ("automation_id", control.AutomationId),
            ("class_name", control.ClassName),
            ("help_text", control.HelpText),
        ):
            if value:
                entry[key] = value
        if kind not in {"ImageControl", "TextControl"}:
            pattern = control.GetLegacyIAccessiblePattern()
            value = pattern.Value if pattern else ""
            if value:
                entry["value"] = value
        controls.append(entry)
    return controls


def probe(win, depth=0, maxdepth=3):
    if depth > maxdepth:
        return
    try:
        kids = win.GetChildren()
    except Exception:
        log.debug("probe failed; using existing fallback", exc_info=True)
        return
    for c in kids[:40]:
        print(
            "  " * depth
            + f"{c.ControlTypeName:22s} class={c.ClassName!r:28} name={(c.Name or '')[:40]!r}"
        )
        probe(c, depth + 1, maxdepth)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--out", help="output file (defaults to the crawl file for the selected mode)"
    )
    ap.add_argument("--only", help="comma separated top menus, e.g. File,Edit")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--probe",
        action="store_true",
        help="print the UI Automation tree of the Illustrator window",
    )
    mode.add_argument(
        "--properties",
        action="store_true",
        help="capture the open Properties panel as JSON",
    )
    ap.add_argument(
        "--context", help="name for the Properties state (default: current)"
    )
    args = ap.parse_args()
    if args.context is not None and (not args.properties or not args.context.strip()):
        ap.error("--context requires --properties and a nonempty state name.")
    if args.properties and args.only is not None:
        ap.error("--only applies to menu crawling.")
    out = Path(args.out) if args.out else (PROPERTIES_OUT if args.properties else OUT)

    with auto.UIAutomationInitializerInThread():
        win = find_illustrator()
        if win is None:
            print(
                "No Illustrator window found. Start Illustrator and open a document first."
            )
            return 1
        if args.probe:
            probe(win)
            return 0
        if args.properties:
            controls = crawl_properties(win)
            if not controls:
                print(
                    "No Properties controls found. Open Window > Properties and try again."
                )
                return 2
            snapshot = {
                "panel": "properties",
                "context": args.context.strip() if args.context else "current",
                "controls": controls,
            }
            out.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                f'wrote {out} ({len(controls)} UIA nodes). Next: python tools/build_contexts.py --properties "{out}"'
            )
            return 0
        only = (
            {x.strip().casefold() for x in args.only.split(",")} if args.only else None
        )
        if only and (unknown := only - {name.casefold() for name in MENU_KEYS}):
            ap.error(f"Unknown top-level menu: {', '.join(sorted(unknown))}")
        if auto.GetForegroundWindow() != win.NativeWindowHandle:
            win.SetActive()
        try:
            lines, missing = crawl_menus(win, only)
        except RuntimeError as exc:
            print(exc)
            return 2
        if missing:
            print(
                f"Could not read these menus: {', '.join(missing)}. The output file was preserved."
            )
            return 3
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        n = sum(1 for l in lines if l.strip() and not l.lstrip().startswith("#"))
        print(
            f'wrote {out}  ({n} lines).  Next:  python tools/build_contexts.py "{out}"'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
