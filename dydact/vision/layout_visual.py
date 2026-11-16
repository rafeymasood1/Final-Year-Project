"""Conservative visual preservation of embedded horizontal toolbars.
This can only add search area; a false positive cannot hide a control.
"""

import cv2
import numpy as np


def embedded_toolbars(frame, canvases, settings):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    neutral = (
        np.ptp(frame.astype(np.int16), axis=2) <= 3
        if frame.ndim == 3
        else np.ones_like(gray, bool)
    )
    result = []
    for color in settings.get("background_values", []):
        mask = (neutral & (np.abs(gray.astype(np.int16) - color) <= 2)).astype(np.uint8)
        for x, y, w, h in canvases:
            x, y, w, h = map(round, (x, y, w, h))
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(gray.shape[1], x + w), min(gray.shape[0], y + h)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = mask[y0:y1, x0:x1]
            count, _labels, stats, _ = cv2.connectedComponentsWithStats(crop)
            for i in range(1, count):
                bx, by, bw, bh, area = stats[i]
                if not (
                    20 <= bh <= settings.get("max_height", 100)
                    and bw >= 4 * bh
                    and area / (bw * bh) >= 0.30
                ):
                    continue
                # Require content distributed across the bar, not merely a flat rectangle.
                pixels = gray[y0 + by : y0 + by + bh, x0 + bx : x0 + bx + bw]
                content = cv2.Canny(pixels, 40, 100)
                if (
                    sum(
                        np.count_nonzero(part) > 10
                        for part in np.array_split(content, 4, axis=1)
                    )
                    < 3
                ):
                    continue
                pad = 3
                result.append(
                    [
                        int(x0 + bx - pad),
                        int(y0 + by - pad),
                        int(bw + 2 * pad),
                        int(bh + 2 * pad),
                    ]
                )
    return [
        box
        for box in result
        if not any(
            other != box
            and other[0] <= box[0]
            and other[1] <= box[1]
            and other[0] + other[2] >= box[0] + box[2]
            and other[1] + other[3] >= box[1] + box[3]
            for other in result
        )
    ]
