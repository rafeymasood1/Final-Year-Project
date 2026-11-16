"""dydact/vision/ocr.py - OCR-based text finding for menu items, via RapidOCR
(ONNX Runtime, CPU, no OS/language-pack dependency - works on any platform).
Runs detection + recognition only (text-direction classification is disabled -
menu text is always upright, so skipping it saves a model pass). Latency is
higher than an OS-accelerated OCR engine, so keep `region` crops small: it's
the main lever on speed here.

Needs: pip install rapidocr onnxruntime   (the import below is `from rapidocr import RapidOCR`)
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

import cv2
import numpy as np

from ..common import norm_label

THR = 0.6  # min text-similarity (difflib ratio, or substring match) to accept
MIN_OCR_DIM = 64  # pad any crop below this on either axis; thin strips (e.g. a menu
# bar crop) can trip an internal reshape/assert in the detector


@dataclass
class TextMatch:
    text: str  # the OCR'd line that matched (may be longer than the search word)
    score: float  # similarity 0-1
    box: list  # [x, y, w, h] in screenshot pixels (full-frame coords)

    @property
    def center(self):
        x, y, w, h = self.box
        return x + w // 2, y + h // 2


def _quad_to_box(quad) -> list:
    """RapidOCR gives 4 corner points [[x,y],...]; collapse to an axis-aligned
    [x, y, w, h] box, which is what overlay.Rect / ClickRect expect."""
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    x0, y0 = min(xs), min(ys)
    return [round(x0), round(y0), round(max(xs) - x0), round(max(ys) - y0)]


class TextFinder:
    """find(frame, text, region) -> best TextMatch or None.
    region = (x, y, w, h) crop of the frame to OCR. Keep this small - latency
    scales with pixel area, and a menu/submenu is never the whole screen."""

    def __init__(self):
        from rapidocr import RapidOCR

        self._engine = RapidOCR()

    def find(
        self,
        frame: np.ndarray,
        text: str,
        region: tuple | None = None,
        thr: float = THR,
    ):
        best = None
        labels = text.split("|")
        for line, box in self._recognize(frame, region):
            score = max(self._match(line, label) for label in labels)
            if score >= thr and (best is None or score > best.score):
                best = TextMatch(line, round(score, 3), box)
        return best

    @staticmethod
    def _match(line: str, text: str) -> float:
        nl, nt = norm_label(line), norm_label(text)
        if not nl or not nt:
            return 0.0
        if nl == nt:
            return 1.0
        line_tokens = re.findall(r"[^\W_]+", line.lower())
        text_tokens = re.findall(r"[^\W_]+", text.lower())
        count = len(text_tokens)
        if count and any(
            line_tokens[i : i + count] == text_tokens
            for i in range(len(line_tokens) - count + 1)
        ):
            return 0.9 - 0.05 * (len(nl) - len(nt)) / len(nl)
        return difflib.SequenceMatcher(None, nl, nt).ratio()

    def _recognize(self, frame: np.ndarray, region: tuple | None):
        """-> [(line_text, [x,y,w,h] in full-frame coords), ...]"""
        x0, y0 = 0, 0
        crop = frame
        if region:
            x0, y0, w, h = region
            x0, y0 = max(0, x0), max(0, y0)
            crop = frame[y0 : y0 + h, x0 : x0 + w]
        if crop.size == 0:
            return []
        ch, cw = crop.shape[:2]
        pad_b = max(
            0, MIN_OCR_DIM - ch
        )  # pad below/right only, so box coords stay correct as-is
        pad_r = max(0, MIN_OCR_DIM - cw)
        if pad_b or pad_r:
            crop = cv2.copyMakeBorder(
                crop, 0, pad_b, 0, pad_r, cv2.BORDER_CONSTANT, value=(255, 255, 255)
            )
        rgb = crop[:, :, ::-1]  # BGR (from mss/cv2) -> RGB, which RapidOCR expects
        out_raw = self._engine(rgb, use_cls=False)
        rows = self._to_rows(out_raw)
        if not rows:
            return []
        out = []
        for quad, line, conf in rows:
            bx, by, bw, bh = _quad_to_box(quad)
            out.append((line, [bx + x0, by + y0, bw, bh]))
        return out

    @staticmethod
    def _to_rows(out_raw):
        """Normalize across rapidocr versions: older releases return a (result, elapse)
        tuple where result is [(box, text, score), ...] or None; newer releases return
        a RapidOCROutput object with .boxes / .txts / .scores attributes instead."""
        if hasattr(out_raw, "boxes"):  # new RapidOCROutput shape
            boxes = out_raw.boxes
            if boxes is None or len(boxes) == 0:
                return []
            return list(zip(boxes, out_raw.txts, out_raw.scores))
        result = out_raw[0] if isinstance(out_raw, tuple) else out_raw  # old shape
        return result or []
