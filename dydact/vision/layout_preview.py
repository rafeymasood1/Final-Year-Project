"""Preview actual canvas exclusions: python -m dydact.vision.layout_preview --app illustrator."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from .capture import ScreenCapture
from .layout import LayoutDetector, plan


def render(frame, layout):
    image = frame.copy()
    excluded = ~layout.mask
    tint = np.full_like(image, (40, 40, 230))
    image[excluded] = cv2.addWeighted(image, 0.55, tint, 0.45, 0)[excluded]
    for box in layout.preserved:
        x, y, w, h = map(round, box)
        cv2.rectangle(image, (x, y), (x + w, y + h), (0, 220, 0), 2)
    cv2.rectangle(image, (0, 0), (min(image.shape[1] - 1, 1100), 57), (25, 25, 25), -1)
    cv2.putText(
        image,
        "RED: excluded | GREEN: preserved UI",
        (12, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        2,
    )
    cv2.putText(
        image,
        layout.status,
        (12, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (255, 255, 255),
        1,
    )
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="illustrator")
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        help="Diagnostic accessibility timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/shots/layout_preview.png")
    )
    parser.add_argument(
        "--shot", type=Path, help="Offline screenshot, paired with --nodes JSON"
    )
    parser.add_argument("--nodes", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if bool(args.shot) != bool(args.nodes):
        parser.error("--shot and --nodes must be supplied together")
    if args.shot:
        frame = cv2.imread(str(args.shot))
        if frame is None:
            parser.error("Could not read screenshot")
        detector = LayoutDetector(args.app, {})
        detector.nodes = json.loads(args.nodes.read_text())
        layout = plan(
            np.ones(frame.shape[:2], bool),
            detector.nodes,
            detector.profile,
            frame=frame,
        )
    else:
        capture = ScreenCapture(args.monitor)
        detector = LayoutDetector(
            args.app, capture.monitor, timeout_seconds=args.timeout
        )
        try:
            frame, visible = capture.grab_app(detector.profile.get("processes", []))
        except RuntimeError as ex:
            parser.exit(1, str(ex) + "\n")
        layout = detector.detect(visible, frame=frame)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(args.output), render(frame, layout)):
        raise RuntimeError("Could not save preview")
    args.output.with_suffix(".nodes.json").write_text(
        json.dumps(detector.nodes, indent=2)
    )
    args.output.with_suffix(".json").write_text(
        json.dumps(
            {
                "status": layout.status,
                "canvases": layout.canvases,
                "preserved": layout.preserved,
                "search_regions": layout.regions,
                "search_pixels": int(layout.mask.sum()),
                "probe_error": detector.error,
            },
            indent=2,
        )
    )
    print(layout.status)
    native = [n for n in detector.nodes if n.get("source") == "win32"]
    print(
        f"Accessibility controls: {len(detector.nodes) - len(native)}; native child windows: {len(native)}"
    )
    if not layout.canvases and detector.nodes:
        print(
            "No configured canvas selector matched. Inspect the .nodes.json inventory before adding a rule."
        )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
