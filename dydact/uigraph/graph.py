"""UI graph built from contexts.json, plus the BFS that turns "I want <target>" into steps.

Nodes
    icon   a toolbox tool or panel button you can find on screen by template matching
           (the node id is the data/refs/<id>/ folder name)
    menu   a menu command (no template: shown as text, e.g. "File > Save  Ctrl+S")

Edges  (src -> dst means: "doing <action> on src makes dst usable")
    long_press   group sibling -> tool in the same flyout
    click        panel-opening icon -> every element of that panel
    menu         menu command -> same panel elements / the Advanced toolbar fallback
    extra edges  the optional top-level "edges" list in contexts.json

Routing = breadth-first search *backwards* from the target. Depth 0 is the target; if it is
already on screen the route is one step. Otherwise every node that could reveal it is
checked (is it on screen?) level by level, and the first reachable one gives the shortest
path. Every edge on that path is one step shown to the user. "On screen?" is a callback, so
the BFS only pays for the template matches it really needs. An optional `prefetch(names)`
callback is called with every icon of a BFS level before they are checked one by one, so the
caller can match them in parallel.

    python -m dydact.uigraph.graph --check                          validate contexts.json
    python -m dydact.uigraph.graph route star_tool                  route with an empty screen model
    python -m dydact.uigraph.graph route add_anchor_point_tool --visible pen,selection
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..common import norm
from ..paths import CONTEXTS_JSON, REFS, themed_pngs

SELECT_HINT = (
    "Make sure something is selected first: pick the Selection tool and click the "
    "artwork (skip this if it already is)."
)


@dataclass(frozen=True)
class Node:
    id: str
    kind: str  # "icon" | "menu"
    does: str = ""
    shortcut: str = ""
    path: tuple = ()  # menu path, e.g. ("File", "Save")
    group: str = ""
    needs_selection: bool = False
    uia_selector: dict[str, str] = field(default_factory=dict)

    @property
    def locatable(self) -> bool:
        return self.kind == "icon"

    def label(self) -> str:
        if self.kind == "menu":
            s = " > ".join(
                p.replace("|", " / ") for p in self.path
            )  # "Show X|Hide X" = label that flips
            return f"{s}  ({self.shortcut})" if self.shortcut else s
        return self.id


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    action: str  # "click" | "long_press" | "menu"
    why: str
    replan: bool = (
        False  # state after this step can't be predicted: route again after it
    )


@dataclass
class Step:
    node: str
    action: str
    why: str
    final: bool = False  # this step is the target itself
    optional: bool = False  # may already be satisfied (e.g. something already selected)
    replan: bool = False
    sets_selection: bool = False


@dataclass
class Route:
    target: str
    steps: list
    reachable: bool = True  # False: not on screen and no known way to reveal it


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    fatal: bool


class ContextError(Exception):
    pass


class UIGraph:
    def __init__(self, data: dict):
        self.app = data.get("app", "")
        self.search = data.get("search", {})
        self.nodes: dict[str, Node] = {}
        self.panel_menus: dict[str, str] = {}
        self.control_scopes: dict[str, dict[str, str]] = {}
        self._in: dict[str, list[Edge]] = defaultdict(list)  # dst -> edges, best first
        self.groups: dict[str, list[str]] = {
            k: list(v) for k, v in data.get("groups", {}).items()
        }
        self.always: list[str] = list(data.get("always", []))
        self.verify_notes: list[str] = list(data.get("verify", []))
        self._build(data)
        self.by_norm = {norm(n): n for n in self.nodes}

    @classmethod
    def load(cls, path: Path = CONTEXTS_JSON) -> UIGraph:
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise ContextError(f"{path} not found")
        except json.JSONDecodeError as e:
            raise ContextError(f"{path} is not valid JSON: {e}")

    # ---- construction -------------------------------------------------------------
    def _build(self, data: dict):
        needs = set(data.get("needs_selection", []))
        menus = data.get("menus", {}).get("items", {})
        panels = data.get("panels", {})
        tab_selectors = {
            p["open_with"]: p["tab_selector"]
            for p in panels.values()
            if p.get("open_with") and p.get("tab_selector")
        }
        for panel in panels.values():
            elements = panel.get("elements", {})
            if isinstance(elements, dict) and panel.get("control_scope"):
                tab_selectors.update(elements)
                self.control_scopes.update(
                    {name: panel["control_scope"] for name in elements}
                )
                contexts = panel.get("contexts", {})
                selected = {
                    name
                    for state, names in contexts.items()
                    if state.endswith("_selected")
                    for name in names
                    if isinstance(name, str)
                }
                unselected = {
                    name
                    for name in contexts.get("no_selection", [])
                    if isinstance(name, str)
                }
                needs.update(selected - unselected)
        self.panel_menus = {
            p["open_with"]: p["menu"]
            for p in panels.values()
            if p.get("tab_selector") and p.get("open_with") and p.get("menu") in menus
        }
        group_of = {i: g for g, members in self.groups.items() for i in members}

        icon_ids: list[str] = []
        for members in self.groups.values():
            icon_ids += members
        icon_ids += self.always
        for p in panels.values():
            icon_ids += p.get("elements", [])
            if p.get("open_with"):
                icon_ids.append(p["open_with"])
        icon_ids += data.get(
            "icons", []
        )  # optional: nodes only reachable through custom edges
        fb = data.get("toolbar_fallback", {})
        if fb.get("icon"):
            icon_ids.append(fb["icon"])
        for n in dict.fromkeys(icon_ids):
            self.nodes[n] = Node(
                n,
                "icon",
                group=group_of.get(n, ""),
                needs_selection=n in needs,
                uia_selector=tab_selectors.get(n, {}),
            )

        for mid, m in menus.items():
            self.nodes[mid] = Node(
                mid,
                "menu",
                does=m.get("does", ""),
                shortcut=m.get("shortcut", ""),
                path=tuple(m.get("path", [])),
                needs_selection=bool(m.get("needs_selection")) or mid in needs,
            )

        # 1. flyouts: any sibling that is on screen can open the flyout holding the target
        for members in self.groups.values():
            for dst in members:
                for src in members:
                    if src != dst:
                        self._edge(
                            Edge(
                                src,
                                dst,
                                "long_press",
                                f"Press and hold {src} to open its flyout; {dst} is inside it.",
                            )
                        )
        # 2. panels: the opener icon, else the Window menu entry
        for name, p in panels.items():
            for el in p.get("elements", []):
                if p.get("open_with"):
                    self._edge(
                        Edge(
                            p["open_with"],
                            el,
                            "click",
                            f"Open the {name.title()} panel; {el} lives there (skip if it is already open).",
                        )
                    )
                if p.get("menu") in self.nodes:
                    self._edge(
                        Edge(
                            p["menu"],
                            el,
                            "menu",
                            f"Open the {name.title()} panel from the Window menu; {el} lives there.",
                        )
                    )
        # 3. Basic toolbar fallback: switch to Advanced, then look again
        fb_menu = fb.get("menu")
        if fb_menu in self.nodes:
            for members in self.groups.values():
                for dst in members:
                    self._edge(
                        Edge(
                            fb_menu,
                            dst,
                            "menu",
                            "Neither this tool nor its neighbours are on screen, so the Basic "
                            "toolbar is probably active. Switch to the Advanced toolbar.",
                            replan=True,
                        )
                    )
        # 4. hand-written extra edges
        for e in data.get("edges", []):
            self._edge(
                Edge(
                    e["from"],
                    e["to"],
                    e.get("action", "click"),
                    e.get("why", ""),
                    bool(e.get("replan", False)),
                )
            )
        for (
            dst,
            edges,
        ) in (
            self._in.items()
        ):  # icons (can be seen) before menu commands (always there)
            edges.sort(key=lambda e: self.nodes[e.src].kind != "icon")

    def _edge(self, e: Edge):
        for end in (e.src, e.dst):
            if end not in self.nodes:
                raise ContextError(f"edge {e.src} -> {e.dst}: unknown node {end!r}")
        self._in[e.dst].append(e)

    # ---- lookups ------------------------------------------------------------------
    def icon_names(self) -> list[str]:
        return [n for n, v in self.nodes.items() if v.kind == "icon"]

    def targets(self) -> list[str]:
        """Everything the LLM may name: every icon and every menu command."""
        return list(self.nodes)

    def resolve(self, name: str) -> str | None:
        if name in self.nodes:
            return name
        return self.by_norm.get(norm(name))

    def edges_into(self, node_id: str) -> list[Edge]:
        return list(self._in.get(node_id, []))

    # ---- BFS ----------------------------------------------------------------------
    def route(
        self,
        target: str,
        visible: Callable[[str], bool],
        assume_selected: bool = False,
        prefetch: Callable[[list], None] | None = None,
    ) -> Route:
        """Steps that take the user from the current screen to using `target`.

        visible(icon_id) -> True when that icon is on screen right now.
        prefetch(icon_ids) -> optional hint: these icons are about to be asked about, so
        the caller may match them all at once (in parallel) and answer visible() from cache.
        """
        tid = self.resolve(target)
        if tid is None:
            raise KeyError(target)
        node = self.nodes[tid]
        if tid in self.panel_menus and not visible(tid):
            return Route(tid, [Step(self.panel_menus[tid], "menu", "", final=True)])
        steps: list[Step] = []
        if (
            node.needs_selection
            and not assume_selected
            and not (node.uia_selector and visible(tid))
        ):
            selection = self.resolve("Select") or "selection"
            steps.append(
                Step(
                    selection, "click", SELECT_HINT, optional=True, sets_selection=True
                )
            )

        chain = self._shortest(tid, visible, prefetch)
        if chain is None:
            steps.append(Step(tid, self._final_action(node), "", final=True))
            return Route(tid, steps, reachable=False)
        for e in chain:
            steps.append(Step(e.src, e.action, e.why, replan=e.replan))
            if e.replan:  # the screen after this is unknowable: stop, route again later
                return Route(tid, steps)
        steps.append(Step(tid, self._final_action(node), "", final=True))
        return Route(tid, steps)

    @staticmethod
    def _final_action(node: Node) -> str:
        return "menu" if node.kind == "menu" else "click"

    def _shortest(
        self,
        target: str,
        visible: Callable[[str], bool],
        prefetch: Callable[[list], None] | None = None,
    ) -> list[Edge] | None:
        """Backward BFS. [] = the target is usable as it is; None = no way to reach it."""
        node = self.nodes[target]
        if node.kind != "icon" or visible(target):
            return []
        seen = {target}
        queue = deque([(target, [])])
        while queue:
            cur, chain = queue.popleft()
            edges = [e for e in self._in.get(cur, []) if e.src not in seen]
            if prefetch:  # let the caller match this whole level at once
                prefetch([e.src for e in edges if self.nodes[e.src].kind == "icon"])
            nxt = []
            for e in edges:
                if e.src in seen:
                    continue
                seen.add(e.src)
                path = [e] + chain
                src = self.nodes[e.src]
                if src.kind != "icon" or visible(e.src):  # menu bar is always there
                    return path
                nxt.append((e.src, path))
            queue.extend(nxt)
        return None

    # ---- text for the LLM / UI ----------------------------------------------------
    def catalog(self, notes: dict[str, str] | None = None) -> str:
        notes = notes or {}
        lines = []
        for n, v in self.nodes.items():
            if v.kind == "menu":
                head = (
                    f"- {n} [menu: {' > '.join(p.replace('|', ' / ') for p in v.path)}"
                    + (f", {v.shortcut}" if v.shortcut else "")
                    + "]"
                )
                desc = v.does
            else:
                head = f"- {n}" + (f" [group: {v.group}]" if v.group else "")
                desc = notes.get(n, "")
            lines.append(f"{head}: {desc}" if desc else head)
        return "\n".join(lines)

    # ---- validation ---------------------------------------------------------------
    def problems(self, refs_dir: Path | None = None) -> list[Problem]:
        out = []
        for n, v in self.nodes.items():
            if v.kind == "icon" and not self._in.get(n) and n not in self.always:
                out.append(
                    Problem(
                        "unreachable_icon",
                        f"{n}: not in 'always' and nothing can reveal it (only reachable if already on screen)",
                        True,
                    )
                )
        for n in self.always:
            if n in self.groups_flat():
                out.append(
                    Problem(
                        "always_in_group",
                        f"{n}: listed in 'always' but also in a group",
                        True,
                    )
                )
        if refs_dir is not None and Path(refs_dir).is_dir():
            missing = [
                n
                for n in self.icon_names()
                if not self.nodes[n].uia_selector
                and not any(themed_pngs(Path(refs_dir) / n))
            ]
            if missing:
                out.append(
                    Problem(
                        "missing_refs",
                        f"{len(missing)} icons have no matching templates yet: {', '.join(missing[:12])}"
                        + (" ..." if len(missing) > 12 else ""),
                        False,
                    )
                )
        return out

    def groups_flat(self) -> set[str]:
        return {i for m in self.groups.values() for i in m}


def format_route(g: UIGraph, r: Route) -> str:
    lines = [f"target: {r.target}" + ("" if r.reachable else "   (NOT REACHABLE)")]
    for i, s in enumerate(r.steps, 1):
        who = g.nodes[s.node].label()
        flag = (
            " [final]"
            if s.final
            else (
                " [optional]" if s.optional else (" [then re-plan]" if s.replan else "")
            )
        )
        lines.append(
            f"  {i}. {s.action:10s} {who}{flag}"
            + (f"\n       {s.why}" if s.why else "")
        )
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    g = UIGraph.load()
    if argv[:1] == ["--check"]:
        n_edges = sum(len(v) for v in g._in.values())
        print(
            f"{len(g.nodes)} nodes ({len(g.icon_names())} icons, {len(g.nodes) - len(g.icon_names())} menu "
            f"commands), {n_edges} edges"
        )
        probs = g.problems(REFS)
        print(
            "\n".join(f"  ! [{p.code}] {p.message}" for p in probs)
            or "  no problems found"
        )
        if g.verify_notes:
            print("Unconfirmed (from contexts.json 'verify'):")
            print("\n".join(f"  ? {v}" for v in g.verify_notes))
        return 1 if any(p.fatal for p in probs) else 0
    if argv[:1] == ["route"] and len(argv) >= 2:
        vis = set()
        if "--visible" in argv:
            vis = {
                g.resolve(x.strip()) or x.strip()
                for x in argv[argv.index("--visible") + 1].split(",")
            }
        else:
            vis = set(g.always)  # default screen model: the 'always' icons
        print(format_route(g, g.route(argv[1], lambda n: n in vis)))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
