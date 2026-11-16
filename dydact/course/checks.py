"""Did the learner really do it?  (Not built yet - this is where it goes.)

Three levels, cheapest first:
  1. click_target     the learner clicked inside the box Guide highlighted (clicks.py - works today)
  2. screen checks    an icon/panel is now visible, or the artboard region changed (vision/ - works today)
  3. document checks  ask Illustrator itself (COM / ExtendScript): "does a star exist?", "is the fill red?"
                      This is the reliable way to verify real work. Add it here as functions
                      registered in CHECKS, e.g.  CHECKS["object_created"] = lambda ctx: ...
"""

from __future__ import annotations

from collections.abc import Callable

CHECKS: dict[str, Callable[[dict], bool]] = {}


def run_check(name: str, ctx: dict) -> bool:
    fn = CHECKS.get(name)
    if fn is None:
        raise KeyError(f"no check called {name!r} in course/checks.py")
    return bool(fn(ctx))
