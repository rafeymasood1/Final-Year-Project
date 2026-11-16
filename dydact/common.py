"""Tiny helpers shared by several packages (used to be copy-pasted)."""


def norm(name: str) -> str:
    """'Pen Tool' / 'pen-tool' / 'pen_tool' -> 'pen';  'Eye Dropper' / 'eyedropper' -> 'eyedropper'."""
    n = "".join(c for c in name.lower() if c.isalnum())
    return n[:-4] if n.endswith("tool") and len(n) > 4 else n


def norm_label(text: str) -> str:
    """Normalize a UI label without stripping words such as 'tool'."""
    return "".join(c for c in text.lower() if c.isalnum())
