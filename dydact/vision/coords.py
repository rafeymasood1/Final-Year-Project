"""Coordinate helpers.

Everything the vision side produces (VLM output, template-match bboxes) is in
*screenshot pixels*. Qt draws in *logical pixels*. On a 150% display
the two differ by dpr=1.5, so every draw call goes through here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    """Box in screenshot pixels, relative to the captured monitor's top-left."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def from_xyxy(cls, x1: float, y1: float, x2: float, y2: float) -> Rect:
        return cls(x1, y1, x2 - x1, y2 - y1)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)


def px_to_logical(value: float, dpr: float) -> float:
    return value / dpr


def normalized_to_px(
    nx: float, ny: float, width: int, height: int
) -> tuple[float, float]:
    """For VLMs that return 0-1 (or 0-1000 pre-divided) coordinates."""
    return nx * width, ny * height
