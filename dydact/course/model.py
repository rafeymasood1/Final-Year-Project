"""Course data: load a lesson JSON and check it. No Qt here, so it is easy to test.

A lesson is a list of steps. A step has a type and some text to say:
    info   text only, the user presses Next
    do     `target` is a node id (or name) from contexts.json; Dydact highlights it on screen and the
           lesson moves on when the user has clicked through to it
    try    the user does something on the canvas (which Dydact cannot see) and presses "I did it"
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STEP_TYPES = ("info", "do", "try")


class CourseError(Exception):
    pass


@dataclass(frozen=True)
class CourseStep:
    type: str
    say: str
    target: str = ""


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    goal: str
    steps: tuple[CourseStep, ...]
    summary: str = ""
    app: str = ""


def load_lesson(path: Path) -> Lesson:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CourseError(f"{path} not found") from None
    except json.JSONDecodeError as e:
        raise CourseError(f"{path} is not valid JSON: {e}") from None
    try:
        steps = tuple(CourseStep(s["type"], s["say"], s.get("target", "")) for s in data["steps"])
        return Lesson(data.get("id", Path(path).stem), data["title"], data.get("goal", ""),
                      steps, data.get("summary", ""), data.get("app", ""))
    except (KeyError, TypeError) as e:
        raise CourseError(f"{path}: missing or malformed field {e}") from None


def problems(lesson: Lesson, known: Callable[[str], bool]) -> list[str]:
    """Human-readable problems; empty list = fine. `known(name)` says whether a target exists."""
    out = []
    if not lesson.steps:
        out.append("lesson has no steps")
    for i, s in enumerate(lesson.steps, 1):
        if s.type not in STEP_TYPES:
            out.append(f"step {i}: unknown type {s.type!r} (use {', '.join(STEP_TYPES)})")
        if not s.say.strip():
            out.append(f"step {i}: empty text")
        if s.type == "do":
            if not s.target:
                out.append(f"step {i}: 'do' step has no target")
            elif not known(s.target):
                out.append(f"step {i}: target {s.target!r} is not in contexts.json")
    return out
