"""Single place that knows where things live on disk."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REFS = DATA / "refs"  # refs/<icon>/<theme>/<state>.png
SHOTS = DATA / "shots"  # labelling screenshots
LABELS = DATA / "labels.json"
CONTEXTS_JSON = DATA / "contexts.json"
CURRICULUM = ROOT / "curriculum"
PROGRESS_FILE = DATA / "progress.json"

# Grayscale button backgrounds in Illustrator, by theme and selection state.
THEME_BACKGROUNDS = {
    "dark": (0x32, 0x1F),
    "medium-dark": (0x53, 0x30),
    "medium-light": (0xB8, 0x96),
    "light": (0xFF, 0xBD),
}
SVG_RENDER_THEMES = frozenset(THEME_BACKGROUNDS)
TEMPLATE_STATES = ("unselected", "selected")


def themed_pngs(folder: Path):
    """Yield existing (theme, state, path) entries for an icon."""
    for theme in THEME_BACKGROUNDS:
        for state in TEMPLATE_STATES:
            path = folder / theme / f"{state}.png"
            if path.is_file():
                yield theme, state, path
