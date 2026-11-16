"""
find_icon.py - find an Illustrator icon on screen by its name (template matching).

Templates come from data/refs/<icon>/<theme>/<state>.png. Boxes are [x, y, w, h] in
screenshot pixels, the same convention as labels.json.

Usage:
    python -m dydact.vision.find_icon pen --shot shots/dark_125L_001.png          # print + show the box
    python -m dydact.vision.find_icon pen,eraser,zoom --shot shots/dark_125s_021.png --save out.png
    python -m dydact.vision.find_icon "Pen Tool" --delay 3                         # live: 3 s to switch to Illustrator
    python -m dydact.vision.find_icon --all --shot shots/dark_125L_025.png         # every icon in refs/
    python -m dydact.vision.find_icon --list                                       # known icon names
    python -m dydact.vision.find_icon eye_dropper,measure_tool --shot s.png --no-show   # not found? prints the best score

From your own code (e.g. to display the box):
    from dydact.vision.find_icon import IconFinder
    finder = IconFinder()
    m = finder.find(img, "pen")          # img = BGR screenshot -> Match or None
    if m:
        x, y, w, h = m.box
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)

Search order for a single result:
  1. Recheck the last confirmed location and verify against look-alikes.
  2. Search the supplied region at the last successful scale, then default/coarse/fine scales.
  3. Stop at the first stage with a verified candidate above the requested threshold.
  4. Optionally try extended scales if the normal search fails.
prepare_frame() reuses the detected theme, refreshing once per second or on frame resize.
The caller supplies toolbar/flyout regions; all returned coordinates are relative to its input.
"""

import argparse
import difflib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from ..common import norm
from ..paths import LABELS, REFS, TEMPLATE_STATES, THEME_BACKGROUNDS, themed_pngs
from ..uigraph.graph import UIGraph

TRIM = 2  # label.py trims 2px off each side of a ref; added back so boxes match labels.json
THR = 0.85  # accept a match at or above this score (real hits scored >= 0.88 in tests)
SURE = 0.95  # a ref from the same condition scores ~1.0; below this we also try other scales
COARSE = [
    0.6,
    0.65,
    0.7,
    0.75,
    0.8,
    0.85,
    0.9,
    0.95,
    1.05,
    1.1,
    1.15,
    1.2,
    1.25,
    1.3,
    1.4,
    1.5,
]
EXTENDED_SCALES = (0.45, 0.5, 0.55, 1.6, 1.65, 1.75, 1.8, 1.9, 2.0)
NEAR = [
    -0.03,
    -0.02,
    -0.01,
    0,
    0.01,
    0.02,
    0.03,
]  # fine scale steps around a coarse/candidate scale
PEAKS = 5  # candidates kept per (ref, scale), so a rejected look-alike has runners-up
PAD = 6  # px of slack when trying other icons' refs on a candidate spot
MARGIN = 0.02  # another icon must beat a candidate by this much to reject it
SCREEN = (1500, 850)  # max window size for the preview
ROI_PAD = 14  # px around an icon's last position that is searched first
PROBES = (
    "Select",
    "AnchorRegionSelect",
    "SimilarSelect",
    "CurvatureTool",
    "ArtboardTool",
    "Magnification",
    "selection",
    "lasso",
    "magic_wand_tool",
    "curvature_pen_tool",
    "artboard_tool",
    "zoom",
)
_COND = re.compile(r"_\d+(?:_alt)?$")
_THEMED_CONDITIONS = {
    f"{theme}_{state}": theme
    for theme in THEME_BACKGROUNDS
    for state in TEMPLATE_STATES
}
_SELECTION_TOOLS = {"selection", "direct_selection", "group_selection_tool"}


def cond_of(stem, icon=None):
    """Extract 'dark_125L' from either 'Select_dark_125L_001' or 'dark_125L_001'."""
    if icon is not None:
        stem = stem.removeprefix(f"{icon}_")
    return _COND.sub("", stem)


@dataclass
class Match:
    icon: str
    score: float
    box: list  # [x, y, w, h] in screenshot pixels
    scale: float  # screenshot size / ref size
    ref: str  # which reference crop matched, e.g. "dark_125L_001"

    @property
    def center(self):
        x, y, w, h = self.box
        return x + w // 2, y + h // 2


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def nms(cands, overlap=0.3):
    keep = []
    for m in sorted(cands, key=lambda m: -m.score):
        if all(iou(m.box, k.box) < overlap for k in keep):
            keep.append(m)
    return keep


def to_gray(img):
    if img.ndim == 2:
        return img
    return cv2.cvtColor(
        img, cv2.COLOR_BGRA2GRAY if img.shape[2] == 4 else cv2.COLOR_BGR2GRAY
    )


def detect_theme(gray, visible=None):
    """Use neutral chrome near the visible window edges, away from the artboard."""
    if visible is None:
        x0, y0, x1, y1 = 0, 0, gray.shape[1], gray.shape[0]
    else:
        rows = np.flatnonzero(visible.any(axis=1))
        cols = np.flatnonzero(visible.any(axis=0))
        if not rows.size:
            return None
        x0, x1, y0, y1 = cols[0], cols[-1] + 1, rows[0], rows[-1] + 1
    band = max(1, min(32, (x1 - x0) // 6, (y1 - y0) // 6))
    regions = (
        np.s_[y0 : y0 + band, x0:x1],
        np.s_[y0 + band : y1, x0 : x0 + band],
        np.s_[y0 + band : y1, x1 - band : x1],
    )
    counts = np.zeros(256, dtype=np.int64)
    for region in regions:
        pixels = gray[region]
        if visible is not None:
            pixels = pixels[visible[region]]
        counts += np.bincount(pixels.ravel(), minlength=256)
    ranked = sorted(
        (counts[normal], theme) for theme, (normal, _) in THEME_BACKGROUNDS.items()
    )
    score, theme = ranked[-1]
    return (
        theme
        if score >= max(8, counts.sum() * 0.15) and score > ranked[-2][0] * 1.25
        else None
    )


def unpad(x, y, w, h, scale):
    """Template hit -> label-style box: add the TRIM margin back, clipped at the image edge."""
    p = TRIM * scale
    x0, y0 = max(0, round(x - p)), max(0, round(y - p))
    return [x0, y0, round(x + w + p) - x0, round(y + h + p) - y0]


def match_scores(
    gray,
    template,
    visible=None,
    *,
    themed=False,
    ignore_flyout_corner=False,
    flyout=False,
):
    """Use signed contrast for themed icons. Flyout rows can have a different palette."""
    mask = None
    pixels = template.size
    if ignore_flyout_corner:
        # Exclude only background pixels where Illustrator can draw its flyout marker.
        h, w = template.shape
        y, x = np.ogrid[:h, :w]
        corner = (w - 1 - x) / (w * 0.24) + (h - 1 - y) / (h * 0.28) <= 1.25
        marker_area = corner & (template == template[0, 0])
        if marker_area.any():
            mask = (~marker_area).astype(np.uint8)
            pixels = cv2.countNonZero(mask)
    if mask is not None and themed and not flyout:
        # Share the image sums between normalized correlation and color error.
        # OpenCV's two masked calls otherwise calculate these repeatedly.
        weights = mask.astype(np.float32)
        ref = template.astype(np.float32)
        mean = float(np.sum(ref * weights)) / pixels
        # Subtract the same offset from both inputs to avoid cancellation in SSD.
        source = gray.astype(np.float32) - mean
        centered = (ref - mean) * weights
        energy = float(np.sum(centered * centered))
        total = cv2.matchTemplate(source, weights, cv2.TM_CCORR)
        squares = cv2.matchTemplate(source * source, weights, cv2.TM_CCORR)
        numerator = cv2.matchTemplate(source, centered, cv2.TM_CCORR)
        variance = np.maximum(squares - total * total / pixels, 0)
        denominator = np.sqrt(variance * energy)
        scores = np.divide(
            numerator,
            denominator,
            out=np.full_like(numerator, np.nan),
            where=denominator > 0,
        )
        error = squares - 2 * numerator + energy
        np.maximum(error, 0, out=error)
        scores -= np.sqrt(error / pixels) / 100
    else:
        scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED, mask=mask)
        if themed and not flyout:
            error = cv2.matchTemplate(gray, template, cv2.TM_SQDIFF, mask=mask)
            np.maximum(error, 0, out=error)
            scores -= np.sqrt(error / pixels) / 100
        elif not themed:
            np.abs(scores, out=scores)
    np.nan_to_num(scores, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
    if visible is not None:
        h, w = template.shape
        center_y, center_x = h // 2, w // 2
        rows, cols = scores.shape
        scores[~visible[center_y : center_y + rows, center_x : center_x + cols]] = -1.0
    return scores


def peaks(
    gray,
    icon,
    stem,
    scale,
    t,
    visible=None,
    themed=False,
    ignore_flyout_corner=False,
    flyout=False,
):
    """Up to PEAKS distinct hits of one (scaled) template on the screenshot."""
    h, w = t.shape
    if min(h, w) < 6 or h > gray.shape[0] or w > gray.shape[1]:
        return []
    res = match_scores(
        gray,
        t,
        visible,
        themed=themed,
        ignore_flyout_corner=ignore_flyout_corner,
        flyout=flyout,
    )
    out = []
    for _ in range(PEAKS):
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score < 0:
            break
        out.append(Match(icon, round(score, 4), unpad(x, y, w, h, scale), scale, stem))
        res[
            max(0, y - h // 2) : y + h // 2 + 1, max(0, x - w // 2) : x + w // 2 + 1
        ] = -1.0  # suppress
    return out


def rival_wins(
    gray,
    m,
    t,
    scale,
    themed=False,
    margin=MARGIN,
    ignore_flyout_corner=False,
    flyout=False,
):
    """Does template t (another icon's ref) fit m's spot clearly better than m does?"""
    th, tw = t.shape
    x, y, w, h = m.box
    rival_w, rival_h = tw + 2 * TRIM * scale, th + 2 * TRIM * scale
    overlap = min(w, rival_w) * min(h, rival_h)
    if overlap / (w * h + rival_w * rival_h - overlap) < 0.5:
        return False
    x0, y0 = (
        max(0, round(x + w / 2 - tw / 2) - PAD),
        max(0, round(y + h / 2 - th / 2) - PAD),
    )
    win = gray[y0 : y0 + th + 2 * PAD, x0 : x0 + tw + 2 * PAD]
    if win.shape[0] < th or win.shape[1] < tw:
        return False
    res = match_scores(
        win, t, themed=themed, ignore_flyout_corner=ignore_flyout_corner, flyout=flyout
    )
    _, score, _, (lx, ly) = cv2.minMaxLoc(res)
    # same spot, not a bigger widget around it
    return (
        score > m.score + margin
        and iou(unpad(x0 + lx, y0 + ly, tw, th, scale), m.box) >= 0.5
    )


class IconFinder:
    def __init__(self, refs_dir=REFS):
        self.search_regions = (
            None  # Optional full-frame [x,y,w,h] crops supplied by layout detector
        )
        self.refs = {}  # icon -> [(stem, gray)]
        root = Path(refs_dir)
        graph = UIGraph.load()
        self.flyout_tools = {
            icon for group in graph.groups.values() if len(group) > 1 for icon in group
        }
        for icon in graph.icon_names():
            for theme, state, path in themed_pngs(root / icon):
                image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    continue
                if image.shape[0] != image.shape[1]:
                    # Rectangular refs are native screenshot crops: preserve their pixels.
                    glyph = image
                else:
                    # Square SVG renders contain a centered 44 x 36 viewport.
                    width = image.shape[1]
                    height = round(width * 36 / 44)
                    top = (image.shape[0] - height) // 2
                    glyph = cv2.resize(
                        image[top : top + height],
                        (44, 36),
                        interpolation=cv2.INTER_AREA,
                    )
                self.refs.setdefault(icon, []).append(
                    (f"{icon}_{theme}_{state}", glyph)
                )
        if not self.refs:
            raise FileNotFoundError(
                f"no reference images in {root}/<icon>/<theme>/<state>.png"
            )
        self.alias = {}
        for icon in self.refs:
            self.alias.setdefault(norm(icon), icon)
        self._scaled = {}  # (icon, ref index, scale) -> resized template
        self._pool = ThreadPoolExecutor()
        self.theme = None
        self._theme_checked = 0.0
        self._frame_shape = None
        self.scale_hint = 1.0  # last scale a swept match needed
        self.scale_confirmed = False
        self._scale_warm_attempted = False
        self.last = {}  # icon -> last box
        self.last_scale = {}  # icon -> scale of that match

    def names(self):
        return sorted(self.refs)

    def resolve(self, name):
        """Icon name as typed -> folder name in refs/ (raises ValueError with suggestions)."""
        if name in self.refs:
            return name
        if norm(name) in self.alias:
            return self.alias[norm(name)]
        close = difflib.get_close_matches(norm(name), list(self.alias), n=3, cutoff=0.6)
        hint = (
            f" Did you mean: {', '.join(self.alias[c] for c in close)}?"
            if close
            else ""
        )
        raise ValueError(f"no refs for icon {name!r}.{hint}")

    # ---- speed: theme and scale -------------------------------------------------------
    def prepare_frame(self, gray, *, visible=None):
        now = time.monotonic()
        changed_size = self._frame_shape != gray.shape
        if changed_size:
            self.last.clear()
            self.last_scale.clear()
            self.scale_hint = 1.0
            self.scale_confirmed = False
            self._scale_warm_attempted = False
            self._frame_shape = gray.shape
        if (
            not changed_size
            and self.theme is not None
            and now - self._theme_checked < 1.0
        ):
            return
        self._theme_checked = now
        theme = detect_theme(gray, visible)
        if theme is not None and theme != self.theme:
            self.theme = theme
            self.scale_hint = 1.0
            self.scale_confirmed = False
            self._scale_warm_attempted = False
            self.last.clear()
            self.last_scale.clear()

    def warm_scale(self, gray, visible=None):
        """Try probes once per frame-size/theme, stopping at the first sure match."""
        if self._scale_warm_attempted:
            return None
        self._scale_warm_attempted = True
        for icon in PROBES:
            if icon not in self.refs:
                continue
            match = self.find(gray, icon, visible=visible)
            if match is not None and match.score >= SURE:
                return match
        return None

    def _idx(self, icon):
        """Use both states of the detected theme, or all states if unknown."""
        lst = self.refs[icon]
        themed = [
            i
            for i, (stem, _) in enumerate(lst)
            if _THEMED_CONDITIONS[cond_of(stem, icon)] == self.theme
        ]
        return themed or list(range(len(lst)))

    def _themed(self, icon, i):
        return cond_of(self.refs[icon][i][0], icon) in _THEMED_CONDITIONS

    def _near_last(self, gray, icon, visible=None, thr=THR):
        """Recheck a small window around the previous hit; callers fall back if it moved."""
        box = self.last.get(icon)
        if not box:
            return None
        x, y, w, h = box
        x0, y0 = max(0, x - ROI_PAD), max(0, y - ROI_PAD)
        region = np.s_[y0 : y + h + ROI_PAD, x0 : x + w + ROI_PAD]
        crop = gray[region]
        local_visible = visible[region] if visible is not None else None
        s = self.last_scale.get(icon, 1.0)
        best = None
        for i in self._idx(icon):
            stem = self.refs[icon][i][0]
            for m in peaks(
                crop,
                icon,
                stem,
                s,
                self._template(icon, i, s),
                local_visible,
                self._themed(icon, i),
                icon in self.flyout_tools,
            )[:1]:
                if best is None or m.score > best.score:
                    best = m
        if best and best.score >= thr:
            bx, by, bw, bh = best.box
            best.box = [bx + x0, by + y0, bw, bh]
            return best
        return None

    # ---- search -------------------------------------------------------------------
    def find(
        self,
        image,
        name,
        thr=THR,
        verify=True,
        *,
        visible=None,
        flyout=False,
        extended_scales=False,
        full_sweep=False,
    ):
        """Best box for one icon, or None if it is not on screen."""
        found = self.find_all(
            image,
            name,
            topk=1,
            thr=thr,
            verify=verify,
            visible=visible,
            flyout=flyout,
            extended_scales=extended_scales,
            full_sweep=full_sweep,
        )
        return found[0] if found else None

    def find_all(
        self,
        image,
        name,
        topk=5,
        thr=THR,
        verify=True,
        *,
        visible=None,
        flyout=False,
        extended_scales=False,
        full_sweep=False,
    ):
        """Up to topk distinct boxes, best first (an icon can be on screen twice,
        e.g. in the toolbar and in the All Tools panel)."""
        icon = self.resolve(name)
        gray = to_gray(image)
        out = []
        m = (
            self._near_last(gray, icon, visible, thr)
            if topk == 1 and not flyout
            else None
        )
        if m and m.score >= thr and not (verify and self._lookalike(gray, m)):
            out = [m]
        else:
            if topk == 1:
                # Verify each stage before spending time on the next scale sweep.
                preferred = (
                    self.last_scale.get(icon, self.scale_hint)
                    if not flyout
                    else self.scale_hint
                )
                tried = set()
                candidates = []
                sweep = (
                    not self.scale_confirmed or extended_scales or full_sweep or flyout
                )
                stages = (
                    [preferred],
                    [1.0],
                    [round(preferred + d, 2) for d in NEAR if d],
                )
                if sweep:
                    stages += (COARSE,)
                for scales in stages:
                    scales = [s for s in scales if s not in tried]
                    tried.update(scales)
                    stage = self._match(gray, icon, scales, visible, flyout=flyout)
                    candidates.extend(stage)
                    out = self._verified(gray, stage, thr, topk, verify, flyout=flyout)
                    if out:
                        break
                if sweep and not out and candidates:
                    scale = max(candidates, key=lambda m: m.score).scale
                    fine = [
                        round(scale + d, 2)
                        for d in NEAR
                        if round(scale + d, 2) not in tried
                    ]
                    out = self._verified(
                        gray,
                        self._match(gray, icon, fine, visible, flyout=flyout),
                        thr,
                        topk,
                        verify,
                        flyout=flyout,
                    )
            else:
                candidates = self.candidates(gray, icon, visible=visible, flyout=flyout)
                out = self._verified(gray, candidates, thr, topk, verify, flyout=flyout)
            if not out and extended_scales:
                candidates = self._match(
                    gray, icon, EXTENDED_SCALES, visible, flyout=flyout
                )
                if candidates:
                    scale = candidates[0].scale
                    nearby = [round(scale + delta, 2) for delta in NEAR if delta]
                    candidates = nms(
                        candidates
                        + self._match(gray, icon, nearby, visible, flyout=flyout)
                    )
                out = self._verified(
                    gray, candidates, max(thr, THR), topk, verify, flyout=flyout
                )
        if out and not flyout:
            b = out[0]
            self.last[icon], self.last_scale[icon] = b.box, b.scale
            self.scale_hint = b.scale
            if b.score >= SURE:
                self.scale_confirmed = True
        return out

    def _verified(self, gray, candidates, threshold, limit, verify, *, flyout=False):
        hits = []
        for match in candidates:
            if match.score < threshold:
                break
            if not verify or not self._lookalike(gray, match, flyout=flyout):
                hits.append(match)
                if len(hits) == limit:
                    break
        return hits

    def candidates(self, gray, icon, *, visible=None, flyout=False):
        """Every candidate box for one icon, best first, before any threshold or look-alike check."""
        cands = self._match(gray, icon, [1.0], visible, flyout=flyout)
        if cands and cands[0].score >= SURE:
            return cands
        if (
            self.scale_hint != 1.0
        ):  # try the scale that worked last time before sweeping
            scales = [round(self.scale_hint + d, 2) for d in NEAR]
            cands = nms(cands + self._match(gray, icon, scales, visible, flyout=flyout))
            if cands and cands[0].score >= THR:
                return cands
        cands += self._match(gray, icon, COARSE, visible, flyout=flyout)
        if cands:
            s0 = max(cands, key=lambda m: m.score).scale
            scales = [round(s0 + d, 2) for d in NEAR if d]
            cands = nms(cands + self._match(gray, icon, scales, visible, flyout=flyout))
        return cands

    def _template(self, icon, i, scale):
        key = (icon, i, scale)
        if key not in self._scaled:
            g = self.refs[icon][i][1]
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            self._scaled[key] = (
                g
                if scale == 1.0
                else cv2.resize(g, None, fx=scale, fy=scale, interpolation=interp)
            )
        return self._scaled[key]

    def _match(self, gray, icon, scales, visible=None, *, flyout=False):
        """Candidate boxes over the refs of one icon at the given scales, best first."""
        jobs = [
            (self.refs[icon][i][0], s, self._template(icon, i, s))
            for i in self._idx(icon)
            for s in scales
        ]
        # matchTemplate releases the GIL, so threads run the scales in parallel (~2.5x faster)
        ignore_flyout_corner = not flyout and icon in self.flyout_tools
        regions = (
            self.search_regions
            if not flyout and self.search_regions is not None
            else [(0, 0, gray.shape[1], gray.shape[0])]
        )

        def search(job):
            stem, scale, template = job
            hits = []
            th, tw = template.shape
            for x, y, w, h in regions:
                # Extra context lets a template straddle a crop edge without losing its center.
                left, top = max(0, x - tw), max(0, y - th)
                right, bottom = (
                    min(gray.shape[1], x + w + tw),
                    min(gray.shape[0], y + h + th),
                )
                crop = gray[top:bottom, left:right]
                mask = np.zeros(crop.shape, dtype=bool)
                mask[y - top : y + h - top, x - left : x + w - left] = True
                if visible is not None:
                    mask &= visible[top:bottom, left:right]
                for m in peaks(
                    crop,
                    icon,
                    stem,
                    scale,
                    template,
                    mask,
                    themed=True,
                    ignore_flyout_corner=ignore_flyout_corner,
                    flyout=flyout,
                ):
                    m.box[0] += left
                    m.box[1] += top
                    cx, cy = m.center
                    if x <= cx < x + w and y <= cy < y + h:
                        hits.append(m)
            return hits

        return nms([m for found in self._pool.map(search, jobs) for m in found])

    def _lookalike(self, gray, m, *, flyout=False):
        """Check rival shapes at the same spot, including at a different size from the candidate."""
        themed = cond_of(m.ref, m.icon) in _THEMED_CONDITIONS
        selection = themed and m.icon in _SELECTION_TOOLS
        if m.score + MARGIN >= 1 and not (selection or flyout):
            return False
        nearby = {1.0} | {round(m.scale + d, 2) for d in NEAR}
        rivals = [
            (icon, i)
            for icon in self.refs
            if icon != m.icon
            for i in self._idx(icon)
            if not themed or self._themed(icon, i)
        ]
        for scales in (nearby, set(COARSE) - nearby):
            jobs = [
                (
                    self._template(icon, i, s),
                    s,
                    self._themed(icon, i),
                    -MARGIN
                    if flyout or (selection and icon in _SELECTION_TOOLS)
                    else MARGIN,
                    not flyout and icon in self.flyout_tools,
                    flyout,
                )
                for icon, i in rivals
                for s in scales
                if flyout
                or m.score + MARGIN < 1
                or (selection and icon in _SELECTION_TOOLS)
            ]
            if any(self._pool.map(lambda j: rival_wins(gray, m, *j), jobs)):
                return True
        return False


# ---------------------------------------------------------------- display
def draw(img, results, truth=None):
    """Green = found box, yellow = labels.json box (when the shot is labeled)."""
    vis = img.copy()
    for b in (truth or {}).values():
        cv2.rectangle(vis, (b[0], b[1]), (b[0] + b[2], b[1] + b[3]), (0, 220, 255), 1)
    for m in results:
        x, y, w, h = m.box
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(
            vis,
            f"{m.icon} {m.score:.2f}",
            (x, max(12, y - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )
    return vis


def show(vis, title="find_icon  (any key to close)"):
    h, w = vis.shape[:2]
    s = min(SCREEN[0] / w, SCREEN[1] / h, 1.0)
    cv2.imshow(title, cv2.resize(vis, None, fx=s, fy=s, interpolation=cv2.INTER_AREA))
    cv2.waitKey(0)
    cv2.destroyAllWindows()


def grab_screen(monitor=1):
    """Screenshot of one monitor (BGR, physical pixels). Boxes found on it are relative to
    that monitor's top-left corner, which is the screen origin for the primary monitor."""
    import mss  # only needed for live capture

    with (
        getattr(mss, "MSS", None) or mss.mss
    )() as sct:  # mss 10 renamed mss.mss -> mss.MSS
        img = np.array(sct.grab(sct.monitors[monitor]))  # BGRA
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def main():
    ap = argparse.ArgumentParser(
        description="Find Illustrator icons on screen by name."
    )
    ap.add_argument(
        "icons", nargs="?", default="", help="name(s), comma separated: pen,eraser"
    )
    ap.add_argument("--shot", help="screenshot to search (default: grab the screen)")
    ap.add_argument(
        "--monitor", type=int, default=1, help="mss monitor for live grab (1 = primary)"
    )
    ap.add_argument(
        "--delay", type=float, default=0, help="seconds to wait before a live grab"
    )
    ap.add_argument("--all", action="store_true", help="search every icon in refs/")
    ap.add_argument(
        "--list", action="store_true", help="print known icon names and exit"
    )
    ap.add_argument("--topk", type=int, default=1, help="max boxes per icon")
    ap.add_argument(
        "--thr", type=float, default=THR, help="min match score (default %(default)s)"
    )
    ap.add_argument("--no-verify", action="store_true", help="keep look-alike matches")
    ap.add_argument(
        "--no-warm-scale",
        "--no-calibrate",  # Backwards-compatible CLI alias.
        dest="no_warm_scale",
        action="store_true",
        help="skip probe-based scale warming",
    )
    ap.add_argument("--save", help="write the annotated screenshot here")
    ap.add_argument(
        "--no-show", action="store_true", help="don't open a preview window"
    )
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    args = ap.parse_args()

    finder = IconFinder()
    if args.list:
        print("\n".join(finder.names()))
        return
    names = (
        finder.names()
        if args.all
        else [n.strip() for n in args.icons.split(",") if n.strip()]
    )
    if not names:
        ap.error("give icon name(s), --all or --list")

    if args.shot:
        img = cv2.imread(args.shot)
        if img is None:
            ap.error(f"can't read {args.shot}")
    else:
        time.sleep(args.delay)
        img = grab_screen(args.monitor)
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    labeled = labels.get(Path(args.shot).name, {}) if args.shot else {}

    gray = to_gray(img)
    finder.prepare_frame(gray)
    if not args.no_warm_scale:
        finder.warm_scale(gray)
        print(
            f"theme: {finder.theme or 'unknown'}, scale: {finder.scale_hint:g}, "
            f"confirmed: {finder.scale_confirmed}",
            file=sys.stderr,
        )

    results, truth, rows = [], {}, []
    for name in names:
        t0 = time.perf_counter()
        try:
            icon = finder.resolve(name)
            found = finder.find_all(
                gray, icon, topk=args.topk, thr=args.thr, verify=not args.no_verify
            )
        except ValueError as e:  # unknown icon name
            print(e, file=sys.stderr)
            continue
        ms = (time.perf_counter() - t0) * 1000
        results += found
        if icon in labeled:
            truth[icon] = labeled[icon]
        if not found:
            best = finder.candidates(gray, icon)[:1]
            rows.append(
                {
                    "icon": icon,
                    "found": False,
                    "best_score": best[0].score if best else None,
                }
            )
            if not args.json:
                hint = (
                    f"  (best candidate {best[0].score:.2f} at {best[0].box})"
                    if best
                    else ""
                )
                print(f"{icon:32s} not found{hint}  ({ms:.0f} ms)")
        for m in found:
            gt = (
                f"  IoU vs label {iou(m.box, labeled[icon]):.2f}"
                if icon in labeled
                else ""
            )
            rows.append(dict(asdict(m), center=m.center, found=True))
            if not args.json:
                print(
                    f"{icon:32s} score {m.score:.3f}  box {m.box}  center {m.center}  "
                    f"scale {m.scale:.2f}  ({ms:.0f} ms){gt}"
                )
    if args.json:
        print(json.dumps(rows, indent=2))

    vis = draw(img, results, truth)
    if args.save:
        cv2.imwrite(args.save, vis)
        print(f"saved {args.save}", file=sys.stderr)
    if not args.no_show:
        show(vis)


if __name__ == "__main__":
    main()
