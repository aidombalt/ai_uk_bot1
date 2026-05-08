"""Дефолтный шаблон уточняющего вопроса (prompts/completeness.py).

Используется CompletenessChecker когда в обращении жильца отсутствуют
ключевые детали для заявки (парадная, квартира, этаж).

Правится через GUI → Промты → completeness_clarification.
"""

from __future__ import annotations

DEFAULT_CLARIFICATION_QUESTION = (
    "Для оперативной передачи заявки специалистам уточните, пожалуйста: "
    "номер парадной (подъезда) и номер квартиры или этаж."
)
