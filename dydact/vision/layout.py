"""Canvas exclusion and rectangular search planning, independent of the icon matcher.
All boxes are [x, y, width, height] in captured-monitor pixels.
"""

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass

import numpy as np

from ..paths import DATA, ROOT


def matches(node, selector):
    return bool(selector) and all(
        re.fullmatch(value, node.get(key, ""), re.IGNORECASE) is not None
        for key, value in selector.items()
    )


def mask_regions(mask):
    """Exact rectangular partition, merging consecutive identical horizontal spans."""
    regions, active = [], {}
    for y, row in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.r_[False, row, False].astype(np.int8)))
        spans = {(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])}
        for span in list(active):
            if span not in spans:
                start = active.pop(span)
                regions.append((span[0], start, span[1] - span[0], y - start))
        for span in spans:
            active.setdefault(span, y)
    for (a, b), start in active.items():
        regions.append((a, start, b - a, mask.shape[0] - start))
    return regions


def paint(mask, box, value):
    x, y, w, h = map(round, box)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(mask.shape[1], x + w), min(mask.shape[0], y + h)
    if x1 > x0 and y1 > y0:
        mask[y0:y1, x0:x1] = value


@dataclass
class Layout:
    mask: np.ndarray
    regions: list
    canvases: list
    preserved: list
    status: str


def plan(visible, nodes, profile, bounds=None, frame=None):
    allowed = visible.copy()
    rows, cols = np.where(visible)
    if not rows.size:
        return Layout(allowed, [], [], [], "No visible app pixels")
    if bounds is None:
        bounds = (
            int(cols.min()),
            int(rows.min()),
            int(cols.max() - cols.min() + 1),
            int(rows.max() - rows.min() + 1),
        )
    bx, by, bw, bh = bounds
    canvases, preserved = [], []
    for node in nodes:
        box = node["box"]
        if any(matches(node, rule) for rule in profile.get("canvas_selectors", [])):
            x, y, w, h = box
            if w * h >= bw * bh * profile.get("minimum_canvas_fraction", 0.08):
                inset = profile.get("canvas_inset_px", 12)
                if w > 2 * inset and h > 2 * inset:
                    canvases.append(
                        [x + inset, y + inset, w - 2 * inset, h - 2 * inset]
                    )
        if node.get("floating", False) or any(
            matches(node, r) for r in profile.get("preserve_selectors", [])
        ):
            preserved.append(box)
    # Verified container/view relationships, rather than a broad window-class guess.
    for rule in profile.get("canvas_viewport_rules", []):
        for container in nodes:
            if not matches(container, rule["container"]):
                continue
            x, y, w, h = container["box"]
            candidates = []
            for view in nodes:
                if not matches(view, rule["view"]):
                    continue
                vx, vy, vw, vh = view["box"]
                if (
                    abs(vx - x) <= 2
                    and abs(vy - y) <= 2
                    and vw < w
                    and vh < h
                    and vw * vh >= w * h * 0.5
                    and vw * vh
                    >= bw * bh * profile.get("minimum_canvas_fraction", 0.08)
                ):
                    candidates.append(view["box"])
            if candidates:
                vx, vy, vw, vh = min(candidates, key=lambda b: b[2] * b[3])
                inset = profile.get("canvas_inset_px", 12)
                canvases.append(
                    [vx + inset, vy + inset, vw - 2 * inset, vh - 2 * inset]
                )
    if frame is not None and profile.get("embedded_toolbars"):
        from .layout_visual import embedded_toolbars

        preserved.extend(
            embedded_toolbars(frame, canvases, profile["embedded_toolbars"])
        )
    for region in profile.get("manual_canvas_regions", []):
        x, y, w, h = region
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x and 0 < h <= 1 - y):
            raise ValueError(
                "manual_canvas_regions must contain normalized [x,y,width,height]"
            )
        canvases.append([bx + x * bw, by + y * bh, w * bw, h * bh])
    for box in canvases:
        paint(allowed, box, False)
    for box in preserved:
        paint(allowed, box, True)
    allowed &= visible
    regions = mask_regions(allowed)
    # Highly fragmented masks remain exact; cap crop dispatch overhead with a bounding crop.
    if len(regions) > 64:
        regions = [(bx, by, bw, bh)]
    return Layout(
        allowed,
        regions,
        canvases,
        preserved,
        "Canvas exclusions active"
        if canvases
        else "Canvas unknown: searching full visible app",
    )


class LayoutDetector:
    def __init__(self, app, monitor, profile=None, *, timeout_seconds=None):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", app):
            raise ValueError("Invalid app profile name")
        path = DATA / "layouts" / (app + ".json")
        self.profile = (
            profile
            if profile is not None
            else (json.loads(path.read_text()) if path.exists() else {})
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else self.profile.get("probe_timeout_seconds", 2)
        )
        self.monitor = monitor
        self.nodes = []
        self.error = ""
        self._key = None
        self._checked = 0

    def _probe(self, *, timeout=None, **options):
        result = subprocess.run(
            [sys.executable, "-m", "dydact.vision.layout_uia"],
            input=json.dumps(
                {
                    "processes": self.profile.get("processes", []),
                    "monitor": self.monitor,
                    **options,
                }
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds if timeout is None else timeout,
            cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "UIA probe failed")
        return json.loads(result.stdout)

    def detect(self, visible, frame=None):
        rows, cols = np.where(visible)
        key = (
            (
                visible.shape,
                int(rows.min()),
                int(rows.max()),
                int(cols.min()),
                int(cols.max()),
            )
            if rows.size
            else (visible.shape,)
        )
        now = time.monotonic()
        if key != self._key or now - self._checked >= self.profile.get(
            "refresh_seconds", 3
        ):
            self._key, self._checked = key, now
            try:
                self.nodes = self._probe()
                self.error = ""
            except subprocess.TimeoutExpired:
                self.nodes = []
                self.error = f"Accessibility probe timed out after {self.timeout_seconds:g}s; canvas detection is unverified"
            except (ValueError, OSError, RuntimeError) as ex:
                self.nodes = []  # Never retain stale exclusions after a failed refresh.
                self.error = str(ex)
        layout = plan(visible, self.nodes, self.profile, frame=frame)
        if self.error:
            layout.status += "; " + self.error
        return layout

    def visible_control(self, selector, visible, *, refresh=False, scope=None):
        try:
            nodes = (
                self._probe(
                    selector=selector,
                    scope=scope,
                    timeout=max(self.timeout_seconds, 4) if scope else None,
                )
                if refresh
                else self.nodes
            )
        except (subprocess.TimeoutExpired, ValueError, OSError, RuntimeError):
            return None, False
        height, width = visible.shape
        scope_visible = False
        for node in nodes:
            is_scope = bool(scope and matches(node, scope))
            if is_scope or matches(node, selector):
                x, y, w, h = node["box"]
                left, top = max(0, x), max(0, y)
                right, bottom = min(width, x + w), min(height, y + h)
                if (
                    left < right
                    and top < bottom
                    and visible[(top + bottom) // 2, (left + right) // 2]
                ):
                    if is_scope:
                        scope_visible = True
                    else:
                        return [left, top, right - left, bottom - top], scope_visible
        return None, scope_visible
