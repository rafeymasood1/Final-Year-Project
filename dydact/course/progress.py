"""Remembers which lessons the learner finished (data/progress.json)."""

from __future__ import annotations

import json

from ..paths import PROGRESS_FILE


def load() -> dict:
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"completed": []}
    if not isinstance(data, dict):
        return {"completed": []}
    completed = data.get("completed")
    if not isinstance(completed, list):
        data["completed"] = []
    return data


def mark_done(lesson_id: str) -> None:
    d = load()
    if lesson_id not in d["completed"]:
        d["completed"].append(lesson_id)
    PROGRESS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
