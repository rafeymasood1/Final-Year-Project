"""Import menu trees and Illustrator Properties snapshots into data/contexts.json.

    python tools/build_contexts.py                              # uses tools/menu_tree.txt
    python tools/build_contexts.py tools/menu_tree.crawled.txt  # tree produced by crawl_menus.py
    python tools/build_contexts.py <tree> --prune               # also drop menu commands not in the tree
    python tools/build_contexts.py <tree> --out other.json
    python tools/build_contexts.py --properties tools/properties.crawled.json
    python tools/build_contexts.py <tree> --properties tools/properties.crawled.json
    python tools/build_contexts.py <tree> --shortcut-export <Illustrator Export Text file>

What it does
  * every leaf command of the tree becomes  "menu.<menu>.<submenu>.<command>"  (same id rule as before,
    so ids you already use - menu.file.save, menu.object.group, ... - do not change)
  * keeps what you wrote by hand: an existing description / needs_selection is NOT overwritten
  * the shortcut and path always come from the tree (the crawl is the truth)
  * each Properties snapshot supplies matching query targets for one named state under
    panels.properties.contexts. Repeat --properties to import multiple snapshots.
  * Illustrator's shortcut export refreshes shortcuts for matching menu commands.
Menu-tree format: see the header of tools/menu_tree.txt.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXTS = ROOT / "data" / "contexts.json"
DEFAULT_TREE = Path(__file__).with_name("menu_tree.txt")
NEEDS_BY_DEFAULT = {
    "Object",
    "Effect",
}  # top menus whose commands mostly act on a selection
MENU_NOTE = (
    "Menu commands come from tools/menu_tree.txt (pre-filled from knowledge) - run "
    "tools/crawl_menus.py on your copy of Illustrator to confirm labels, shortcuts and submenus."
)
LIVE_HEADER = "# Crawled from the live Illustrator menus."
LIVE_NOTE = (
    "Native menu paths and shortcuts were captured from live Illustrator popups. "
    "The crawl omits open and recent document names and installed and recent font lists. "
    "Commands retained from previous builds may require a separate check."
)
EXPORT_MENUS = (
    "File",
    "Edit",
    "Object",
    "Type",
    "Select",
    "Effect",
    "View",
    "Window",
    "Help",
)
EXPORT_FOOTERS = {
    "Other Select",
    "Other Text",
    "Other Object",
    "Other Panel",
    "Other Misc",
}
EXPORT_SHORTCUT = re.compile(
    r"(?:(?:(?:Alt|Shift|Ctrl)\+)+\S|(?:(?:Alt|Shift|Ctrl)\+)*F(?:1[0-9]|2[0-4]|[1-9]))$"
)
EXPORT_NOTE = (
    "Illustrator's shortcut export verifies shortcut assignments by command label. "
    "The export does not contain submenu nesting."
)


def slug(label: str) -> str:
    first = label.split("|")[0]
    if re.fullmatch(r"[-−]\d+", first.strip()):
        return "minus_" + first.strip()[1:]
    s = re.sub(r"[^a-z0-9]+", "_", first.lower().replace("...", "")).strip("_")
    return s or "item"


def parse_tree(text: str) -> list[dict]:
    rows = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = (len(raw) - len(raw.lstrip(" "))) // 2
        parts = [p.strip() for p in re.split(r"(?<=\s)\|(?=\s|$)", raw.strip())]
        parts += [""] * (4 - len(parts))
        rows.append(
            {
                "indent": indent,
                "label": parts[0],
                "shortcut": parts[1],
                "flags": parts[2],
                "does": parts[3],
            }
        )
    stack: list[str] = []
    items = []
    for i, r in enumerate(rows):
        del stack[r["indent"] :]
        if r["indent"] > len(stack):
            raise ValueError(f"bad indentation at {r['label']!r}")
        stack.append(r["label"])
        nxt = rows[i + 1]["indent"] if i + 1 < len(rows) else 0
        if nxt > r["indent"]:
            continue  # submenu header, not a command
        items.append(
            {
                "path": list(stack),
                "shortcut": r["shortcut"],
                "flags": r["flags"],
                "does": r["does"],
            }
        )
    return items


def build(items: list[dict], old: dict, prune: bool) -> tuple[dict, list[str]]:
    notes, out, seen = [], {}, {}
    for it in items:
        mid = "menu." + ".".join(slug(p) for p in it["path"])
        if mid in seen:
            n = 2
            while f"{mid}_{n}" in seen:
                n += 1
            notes.append(
                f"duplicate id {mid} for {' > '.join(it['path'])} -> {mid}_{n}"
            )
            mid = f"{mid}_{n}"
        seen[mid] = it
        prev = old.get(mid, {})
        path = it["path"]
        previous_path = prev.get("path", [])
        if len(previous_path) == len(path):
            path = [
                old_label
                if "|" in old_label and label in old_label.split("|")
                else label
                for label, old_label in zip(path, previous_path)
            ]
        entry = {"path": path}
        does = prev.get("does") or it["does"]
        if does:
            entry["does"] = does
        sc = it["shortcut"] or prev.get("shortcut", "")
        if sc:
            entry["shortcut"] = sc
        if "s" in it["flags"]:
            needs = True
        elif "-" in it["flags"]:
            needs = False
        elif "needs_selection" in prev:
            needs = bool(prev["needs_selection"])
        else:
            needs = it["path"][0] in NEEDS_BY_DEFAULT
        if needs or "-" in it["flags"] or "needs_selection" in prev:
            entry["needs_selection"] = needs
        out[mid] = entry
    legacy = [m for m in old if m not in out]
    for m in legacy:
        if prune:
            notes.append(f"pruned (not in tree): {m}")
        else:
            out[m] = old[m]
            notes.append(
                f"kept legacy entry not in tree: {m}  {' > '.join(old[m].get('path', []))}"
            )
    return out, notes


# ---- readable JSON (keeps groups/panels tidy, one menu command per line) ----------------------
def _scalar(v):
    return not isinstance(v, (dict, list))


def dump(o, ind=0, width=110) -> str:
    pad, pad2 = " " * ind, " " * (ind + 2)
    if isinstance(o, dict):
        if not o:
            return "{}"
        if "path" in o and all(
            _scalar(v) or (isinstance(v, list) and all(_scalar(x) for x in v))
            for v in o.values()
        ):
            return json.dumps(o, ensure_ascii=False)  # one menu command per line
        body = ",\n".join(
            f"{pad2}{json.dumps(k)}: {dump(v, ind + 2, width)}" for k, v in o.items()
        )
        return "{\n" + body + "\n" + pad + "}"
    if isinstance(o, list):
        if all(_scalar(x) for x in o):
            one = json.dumps(o, ensure_ascii=False)
            if len(one) + ind <= width:
                return one
            lines, cur = [], ""
            for x in o:
                piece = json.dumps(x, ensure_ascii=False) + ","
                if cur and len(pad2 + cur + " " + piece) > width:
                    lines.append(pad2 + cur)
                    cur = piece
                else:
                    cur = f"{cur} {piece}".strip()
            lines.append(pad2 + cur)
            lines[-1] = lines[-1].rstrip(",")
            return "[\n" + "\n".join(lines) + "\n" + pad + "]"
        return (
            "[\n"
            + ",\n".join(pad2 + dump(x, ind + 2, width) for x in o)
            + "\n"
            + pad
            + "]"
        )
    return json.dumps(o, ensure_ascii=False)


def update_menus(data: dict, tree: Path, prune: bool) -> tuple[int, int, list[str]]:
    menus = data.get("menus", {})
    text = tree.read_text(encoding="utf-8")
    items = parse_tree(text)
    if not items:
        raise ValueError("The menu tree has no commands.")
    new_items, notes = build(items, menus.get("items", {}), prune)
    linked = [data.get("toolbar_fallback", {}).get("menu")]
    linked.extend(panel.get("menu") for panel in data.get("panels", {}).values())
    linked.extend(edge[end] for edge in data.get("edges", []) for end in ("from", "to"))
    removed_links = sorted(
        {
            menu
            for menu in linked
            if menu in menus.get("items", {}) and menu not in new_items
        }
    )
    if removed_links:
        raise ValueError(
            f"Pruning would remove menu commands used by the guide: {', '.join(removed_links)}"
        )
    bar = list(dict.fromkeys(item["path"][0] for item in new_items.values()))
    data["menus"] = {"bar": bar, "items": new_items}
    verify = data.setdefault("verify", [])
    if text.startswith(LIVE_HEADER):
        verify[:] = [
            note
            for note in verify
            if not note.startswith(
                ("Menu commands come from", "Illustrator's shortcut export")
            )
        ]
        if LIVE_NOTE not in verify:
            verify.append(LIVE_NOTE)
        # Built entries come first, in the same order as the observed leaves.
        data["menus"]["unverified_items"] = list(new_items)[len(items) :]
    elif not any(v.startswith("Menu commands come from") for v in verify):
        verify.append(MENU_NOTE)
    return len(new_items), len(bar), notes


def properties_targets(panel: dict, controls: list[dict]):
    elements = panel.get("elements", {})
    if not isinstance(elements, dict):
        return controls
    return [
        name
        for name, selector in elements.items()
        if any(
            all(
                re.fullmatch(value, row.get(key, ""), re.IGNORECASE)
                for key, value in selector.items()
            )
            for row in controls
            if row.get("enabled", True) and not row.get("offscreen", False)
        )
    ]


def update_properties(data: dict, path: Path) -> tuple[str, int]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or snapshot.get("panel") != "properties":
        raise ValueError(f"{path}: expected a Properties crawl.")
    context = snapshot.get("context")
    controls = snapshot.get("controls")
    if not isinstance(context, str) or not context.strip():
        raise ValueError(f"{path}: expected a nonempty context name.")
    if (
        not isinstance(controls, list)
        or not controls
        or any(
            not isinstance(control, dict)
            or not isinstance(control.get("name"), str)
            or not isinstance(control.get("control_type"), str)
            or type(control.get("depth")) is not int
            or control["depth"] < 1
            for control in controls
        )
    ):
        raise ValueError(f"{path}: expected a nonempty list of UIA controls.")

    panel = data.setdefault("panels", {}).setdefault("properties", {})
    targets = properties_targets(panel, controls)
    panel.setdefault("contexts", {})[context.strip()] = targets
    return context.strip(), len(targets)


def export_shortcuts(path: Path) -> dict[tuple[str, str], str]:
    """Read English Illustrator's flat Menu Commands export."""
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    rows = [line.strip() for line in raw.decode(encoding).splitlines() if line.strip()]
    if "Menu Commands" not in rows:
        raise ValueError(f"{path}: expected a Menu Commands section.")
    rows = rows[rows.index("Menu Commands") + 1 :]
    end = next(
        (i for i, label in enumerate(rows) if label in EXPORT_FOOTERS), len(rows)
    )
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    # Scan backwards because labels such as Type also occur inside earlier menus.
    for menu in reversed(EXPORT_MENUS):
        start = next((i for i in range(end - 1, -1, -1) if rows[i] == menu), None)
        if start is None:
            raise ValueError(f"{path}: no {menu} heading in the Menu Commands section.")
        for row in rows[start + 1 : end]:
            match = EXPORT_SHORTCUT.search(row)
            name = row[: match.start()].strip() if match else row
            if name:
                found[(slug(menu), slug(name))].append(match.group() if match else "")
        end = start
    return {
        key: values[0]
        for key, values in found.items()
        if len(values) == 1 and values[0]
    }


def update_shortcuts(data: dict, path: Path) -> tuple[int, int]:
    shortcuts = export_shortcuts(path)
    items = data.get("menus", {}).get("items", {})
    keys = {
        menu_id: {(slug(parts[0]), slug(name)) for name in parts[-1].split("|")}
        for menu_id, item in items.items()
        if len(parts := item.get("path", [])) >= 2
    }
    counts = Counter(key for candidates in keys.values() for key in candidates)
    matched = changed = 0
    for menu_id, candidates in keys.items():
        matches = {
            shortcuts[key]
            for key in candidates
            if counts[key] == 1 and key in shortcuts
        }
        if len(matches) == 1:
            shortcut = matches.pop()
            matched += 1
            if items[menu_id].get("shortcut") != shortcut:
                items[menu_id]["shortcut"] = shortcut
                changed += 1
    if not matched:
        raise ValueError(
            f"{path}: no unambiguous menu shortcuts matched the context paths."
        )
    verify = data.setdefault("verify", [])
    if EXPORT_NOTE not in verify:
        verify.append(EXPORT_NOTE)
    return matched, changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("tree", nargs="?", type=Path)
    ap.add_argument(
        "--prune",
        action="store_true",
        help="drop menu commands that are not in the tree",
    )
    ap.add_argument(
        "--properties",
        type=Path,
        action="append",
        default=[],
        help="import a Properties JSON snapshot (repeat for multiple states)",
    )
    ap.add_argument(
        "--shortcut-export",
        type=Path,
        help="refresh matching shortcuts from Illustrator's Keyboard Shortcuts > Export Text file",
    )
    ap.add_argument("--out", type=Path, default=CONTEXTS)
    args = ap.parse_args()
    importing = bool(args.properties or args.shortcut_export)
    if args.prune and importing and args.tree is None:
        ap.error(
            "--prune requires a menu tree when importing a snapshot or shortcut export."
        )

    src = args.out if args.out.exists() else CONTEXTS
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        menu_result = None
        if args.tree is not None or not importing:
            menu_result = update_menus(data, args.tree or DEFAULT_TREE, args.prune)
        properties_results = [update_properties(data, path) for path in args.properties]
        shortcut_result = (
            update_shortcuts(data, args.shortcut_export)
            if args.shortcut_export
            else None
        )
        args.out.write_text(dump(data) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        ap.error(str(exc))

    if menu_result is not None:
        count, menus, notes = menu_result
        print(f"{count} menu commands in {menus} menus -> {args.out}")
        for note in notes:
            print("  -", note)
    for context, count in properties_results:
        print(f"{count} Properties entries for {context!r} -> {args.out}")
    if shortcut_result is not None:
        matched, changed = shortcut_result
        print(f"{matched} menu shortcuts matched, {changed} updated -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
