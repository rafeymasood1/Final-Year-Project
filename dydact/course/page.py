"""The Course mode page shown inside the D button's card (see ui/frontend.py).

Pure view: it shows what the runner tells it to and emits next/back. All logic is in runner.py.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ..overlay.overlay import ACCENT

PRIMARY = (f"QPushButton {{ background: {ACCENT.name()}; color: white; border-radius: 6px;"
           f" padding: 7px 16px; font: 600 10pt 'Segoe UI'; }}"
           f"QPushButton:hover {{ background: {ACCENT.lighter(115).name()}; }}"
           "QPushButton:disabled { background: #3A3A4A; color: #6A6A80; }")
SECONDARY = ("QPushButton { background: transparent; color: #B8B8CC; border: 1px solid #44445A;"
             " border-radius: 6px; padding: 7px 14px; font: 10pt 'Segoe UI'; }"
             "QPushButton:hover { color: white; border-color: #6A6A88; }"
             "QPushButton:disabled { color: #55556A; border-color: #33334A; }")
BAR = (f"QProgressBar {{ background: #2A2A36; border: none; border-radius: 3px; }}"
       f"QProgressBar::chunk {{ background: {ACCENT.name()}; border-radius: 3px; }}")
SELECTOR = ("QComboBox { background: #2A2A36; color: #E6E6F0; border: 1px solid #44445A;"
            " border-radius: 5px; padding: 5px 7px; }"
            "QComboBox QAbstractItemView { background: #2A2A36; color: #E6E6F0;"
            " selection-background-color: #5E3FE0; }")


class CoursePage(QWidget):
    next_clicked = Signal()
    back_clicked = Signal()
    course_changed = Signal(str)
    module_changed = Signal(str)

    def __init__(self, wrapped: list, relayout: Callable[[], None]):
        """`wrapped`: the card's list of word-wrapped labels (it sets their heights);
        `relayout`: call after changing text so the card re-docks above the taskbar."""
        super().__init__()
        self._relayout = relayout
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.app_label = QLabel("", objectName="note")
        self.course_selector = QComboBox()
        self.module_selector = QComboBox()
        self.course_selector.setStyleSheet(SELECTOR)
        self.module_selector.setStyleSheet(SELECTOR)
        self.course_selector.currentIndexChanged.connect(self._emit_course)
        self.module_selector.currentIndexChanged.connect(self._emit_module)
        lay.addWidget(self.app_label)
        selectors = QHBoxLayout()
        selectors.addWidget(self.course_selector, 1)
        selectors.addWidget(self.module_selector, 1)
        lay.addLayout(selectors)

        self.lesson = QLabel("", objectName="note", wordWrap=True)
        self.counter = QLabel("", objectName="note")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setStyleSheet(BAR)
        self.text = QLabel("", wordWrap=True)
        self.text.setStyleSheet("font: 11pt 'Segoe UI'; color: white;")
        self.hint = QLabel("", objectName="status", wordWrap=True)   # what the guide is saying
        self.hint.hide()
        wrapped.extend([self.lesson, self.text, self.hint])

        self.back_btn = QPushButton("Back", cursor=Qt.PointingHandCursor)
        self.next_btn = QPushButton("Next", cursor=Qt.PointingHandCursor)
        self.back_btn.setStyleSheet(SECONDARY)
        self.next_btn.setStyleSheet(PRIMARY)
        self.back_btn.clicked.connect(self.back_clicked)
        self.next_btn.clicked.connect(self.next_clicked)
        row = QHBoxLayout()
        row.addWidget(self.back_btn)
        row.addStretch()
        row.addWidget(self.next_btn)

        lay.addWidget(self.lesson)
        lay.addWidget(self.counter)
        lay.addWidget(self.bar)
        lay.addWidget(self.text)
        lay.addWidget(self.hint)
        lay.addLayout(row)

    def set_catalog(self, app_title: str, courses: list[tuple[str, str]]):
        """Populate the app's course list. Course/module IDs are stored as item data."""
        self.app_label.setText(app_title)
        self.course_selector.blockSignals(True)
        self.course_selector.clear()
        for course_id, title in courses:
            self.course_selector.addItem(title, course_id)
        self.course_selector.blockSignals(False)

    def set_modules(self, modules: list[tuple[str, str]]):
        self.module_selector.blockSignals(True)
        self.module_selector.clear()
        for module_id, title in modules:
            self.module_selector.addItem(title, module_id)
        self.module_selector.blockSignals(False)

    def _emit_course(self, index: int):
        value = self.course_selector.itemData(index) if index >= 0 else None
        if value is not None:
            self.course_changed.emit(str(value))

    def _emit_module(self, index: int):
        value = self.module_selector.itemData(index) if index >= 0 else None
        if value is not None:
            self.module_changed.emit(str(value))

    def set_state(self, lesson: str, counter: str, text: str, progress: tuple[int, int],
                  back_enabled: bool, next_label: str, next_enabled: bool = True):
        self.lesson.setText(lesson)
        self.counter.setText(counter)
        self.bar.setRange(0, max(1, progress[1]))
        self.bar.setValue(progress[0])
        self.text.setText(text)
        self.back_btn.setEnabled(back_enabled)
        self.next_btn.setText(next_label)
        self.next_btn.setEnabled(next_enabled)
        self.set_hint("")          # also re-docks the card

    def set_hint(self, text: str, kind: str = "info"):
        """Show the guide's current message under the lesson text."""
        self.hint.setText(text)
        self.hint.setProperty("kind", kind)
        self.hint.style().unpolish(self.hint)
        self.hint.style().polish(self.hint)
        self.hint.setVisible(bool(text))
        self._relayout()           # the label's height changed: re-dock above the taskbar
