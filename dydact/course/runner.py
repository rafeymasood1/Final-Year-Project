"""Plays a lesson: shows each step on the Course page and, for 'do' steps, asks the Guide to
highlight the target on screen. The lesson advances by itself when the Guide finishes the route.

The lesson file is re-read every time Course mode is opened, so you can edit the JSON and
see the change without restarting Dydact.
"""
from __future__ import annotations

import logging
import json
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

from ..paths import CURRICULUM
from .model import CourseError, Lesson, load_lesson, problems

log = logging.getLogger(__name__)

CATALOG = CURRICULUM / "catalog.json"
ADVANCE_MS = 700          # pause after a finished 'do' step so the user sees it was accepted
NEXT_LABEL = {"info": "Next", "try": "I did it", "do": "Skip"}


class CourseRunner(QObject):
    def __init__(self, box, guide, graph, catalog_path: Path = CATALOG):
        super().__init__()
        self.box, self.guide, self.graph = box, guide, graph
        self.page = box.course_page
        self.catalog_path = Path(catalog_path)
        self.app_id = str(graph.app).strip().lower()
        self.app_root = CURRICULUM / self.app_id
        self.catalog: dict = {}
        self.course_id = ""
        self.module_id = ""
        self.path: Path | None = None
        self.lesson: Lesson | None = None
        self.i = 0                       # index of the step shown; == len(steps) means finished
        self.active = False
        self.mode = ""
        self._waiting: str | None = None  # node id the guide is currently guiding to

        box.mode_changed.connect(self._on_mode)
        self.page.next_clicked.connect(self.next)
        self.page.back_clicked.connect(self.back)
        self.page.course_changed.connect(self._select_course)
        self.page.module_changed.connect(self._select_module)
        guide.route_finished.connect(self._route_finished)
        self._load_catalog()

    def _load_catalog(self):
        """Resolve the active app from contexts.json's UIGraph, then expose its catalog."""
        try:
            data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            apps = data["apps"]
            app = apps[self.app_id]
            if not isinstance(app, dict):
                raise ValueError(f"catalog entry for {self.app_id!r} must be an object")
            courses = app["courses"]
            if not isinstance(courses, dict) or not courses:
                raise ValueError("the app has no courses")
            if any(not isinstance(course, dict) for course in courses.values()):
                raise ValueError("each course entry must be an object")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            self.active = False
            detail = f"Could not load curriculum for {self.app_id or 'the active app'}: {e}"
            log.error(detail)
            self.page.set_state("Course mode", "", "No course is available for this app.",
                                (0, 1), False, "Retry")
            self.page.set_hint(detail, "error")
            return

        self.catalog = courses
        self.page.set_catalog(
            app.get("title", self.app_id.title()),
            [(course_id, course.get("title", course_id.title()))
             for course_id, course in courses.items()],
        )
        self._select_course(str(next(iter(courses))))

    def _select_course(self, course_id: str):
        course = self.catalog.get(course_id)
        if course is None:
            return
        self.course_id = course_id
        modules = course.get("modules", [])
        if not isinstance(modules, list):
            modules = []
        valid_modules = [module for module in modules
                         if isinstance(module, dict) and module.get("id")]
        self.page.set_modules([
            (str(module["id"]), str(module.get("title", module["id"].title())))
            for module in valid_modules
        ])
        if not valid_modules:
            self.path = None
            self.active = False
            self.page.set_state(course.get("title", course_id), "", "This course has no modules.",
                                (0, 1), False, "Start", False)
            return
        self._select_module(str(valid_modules[0]["id"]))

    def _select_module(self, module_id: str):
        course = self.catalog.get(self.course_id, {})
        module = next((item for item in course.get("modules", [])
                       if isinstance(item, dict) and str(item.get("id")) == module_id), None)
        if module is None:
            return
        lesson_rel = module.get("lesson")
        if not isinstance(lesson_rel, str) or not lesson_rel:
            self.path = None
            self.active = False
            self.page.set_state(module.get("title", module_id), "", "This module has no lesson file.",
                                (0, 1), False, "Retry", False)
            return
        candidate = (self.app_root / lesson_rel).resolve()
        try:
            candidate.relative_to(self.app_root.resolve())
        except ValueError:
            self.path = None
            self.active = False
            self.page.set_state(module.get("title", module_id), "", "The lesson path is outside this app's curriculum.",
                                (0, 1), False, "Retry", False)
            return
        self.module_id = module_id
        self.path = candidate
        self.lesson = None
        self.active = False
        if self.mode == "course":
            self.start()
        else:
            self.page.set_state(module.get("title", module_id), "", "Selected module. Open Course mode to begin.",
                                (0, 1), False, "Start")

    # ---- entering / leaving Course mode ----
    def _on_mode(self, mode: str):
        self.mode = mode
        if mode == "course":
            self._render() if self.active else self.start()
        elif mode == "query":
            self.stop()          # (Settings does not interrupt a lesson)

    def start(self):
        if self.path is None:
            self._load_catalog()
            if self.path is None:
                return
        try:
            self.lesson = load_lesson(self.path)
        except CourseError as e:
            self.active = False
            self.page.set_state("Course mode", "", "The lesson could not be loaded.", (0, 1), False,
                                "Retry")
            self.page.set_hint(str(e), "error")
            self.page.next_btn.setEnabled(True)
            return
        if self.lesson.app and self.lesson.app.strip().lower() != self.app_id:
            self.active = False
            detail = (f"Lesson declares app {self.lesson.app!r}, but the active app is "
                      f"{self.app_id!r}.")
            self.page.set_state(self.lesson.title, "", "This lesson is for another app.",
                                (0, 1), False, "Retry")
            self.page.set_hint(detail, "error")
            return
        errors = problems(self.lesson, lambda n: self.graph.resolve(n) is not None)
        if errors:
            log.warning("lesson problems: %s", errors)
            self.active = False
            self.page.set_state(self.lesson.title, "", "This lesson has problems:", (0, 1), False, "Retry")
            self.page.set_hint("\n".join(errors), "error")
            return
        self.active = True
        self.i = 0
        self._render()

    def stop(self):
        if self.active and self._waiting is not None:
            self.guide.clear()
        self.active = False
        self._waiting = None

    # ---- navigation ----
    def next(self):
        if not self.active:
            return self.start()      # "Retry" / "Restart"
        if self.i >= len(self.lesson.steps):
            return self.start()      # finished -> Restart
        self._move(self.i + 1)

    def back(self):
        if self.active and self.i > 0:
            self._move(self.i - 1)

    def _move(self, i: int):
        self.i = i
        self._render()

    def _route_finished(self, node_id: str):
        if not (self.active and self._waiting and node_id == self._waiting):
            return
        self._waiting = None
        i = self.i
        self.page.set_hint("Nice - that's it.", "ok")
        QTimer.singleShot(ADVANCE_MS, lambda: self._move(i + 1) if self.active and self.i == i else None)

    # ---- drawing the current step ----
    def _render(self):
        lesson, n = self.lesson, len(self.lesson.steps)
        if self._waiting is not None or self.guide.target is not None:
            self.guide.clear()       # drop the previous step's highlight and queued work
        self._waiting = None
        if self.i >= n:
            self.page.set_state(lesson.title, "Lesson complete", lesson.summary or "Well done!",
                                (n, n), True, "Restart")
            return
        step = lesson.steps[self.i]
        last = self.i == n - 1 and step.type != "do"
        self.page.set_state(lesson.title, f"Step {self.i + 1} of {n}", step.say, (self.i, n),
                            self.i > 0, "Finish" if last else NEXT_LABEL[step.type])
        if step.type == "do":
            self._waiting = self.graph.resolve(step.target)
            self.guide.search(step.target)
