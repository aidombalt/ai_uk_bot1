"""Детектор дробного троллинга (split-message bypass).

Проблема: жилец обходит модерацию, разбивая мат/оскорбление на куски:
  - msg1: «УК»
  - msg2: «это»
  - msg3: «ху»
  - msg4: «ёвая»
Каждое отдельно — невинно. Склейка за минуту — мат и оскорбление.

Решение: per-(chat_id, user_id) кольцо последних 5 сообщений за 60 секунд.
При каждом новом склеиваем и прогоняем через mat-регексы. Если в склейке мат
есть, а в каждом отдельном сообщении — нет, это **намеренный обход**.

Возвращаем список mid'ов всех частей, чтобы вызывающий код мог удалить
и применить страйк.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from balt_dom_bot.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class FragmentEntry:
    message_id: str
    text: str
    received_at: float


# Жёсткие маты — детектируем по корням, чтобы поймать формы.
# Намеренно компактный список — фокус на самом грубом.
# Каждый паттерн — это re-pattern для поиска ВНУТРИ склееной строки
# (с пробелами или без).
_PROFANITY_ROOTS = (
    r"ху[йеяёюи]",
    r"пизд",
    r"бля[дт]?",
    r"еба[лнт]",
    r"ёб[аеёлнт]",
    r"ебан",
    r"мудак",
    r"мраз",
    r"гнид",
    r"скот",  # «скоты», «скотина»
    r"урод",
    r"свол[оч]",
    r"конч",  # «конченые»
)

_PROFANITY_PATTERN = re.compile(
    "|".join(_PROFANITY_ROOTS), re.IGNORECASE | re.UNICODE,
)


def _has_profanity(text: str) -> bool:
    """Простой детектор мата. Чувствителен к корням слов.

    Проверяет ДВЕ версии текста:
    1) Оригинал — ловит обычный мат («хуёвая», «пиздец»)
    2) Без пробелов и пунктуации — ловит дробление через пробелы:
       «ху ёвая» → «хуёвая» → срабатывает.
    """
    if not text:
        return False
    if _PROFANITY_PATTERN.search(text):
        return True
    # Слитная версия: убираем все не-буквенные символы.
    compact = re.sub(r"[^а-яёa-z]", "", text.lower())
    return bool(_PROFANITY_PATTERN.search(compact))


class FragmentTrollDetector:
    """Детектор дробного троллинга.

    Хранит per-(chat,user) кольцо последних N сообщений за окно T секунд.
    При вызове detect_in_recent проверяет: содержит ли СКЛЕЙКА мат, которого
    НЕТ в каждом отдельном сообщении.
    """

    MAX_FRAGMENTS = 5           # сколько последних сообщений учитывать
    WINDOW_SECONDS = 60         # окно склейки

    def __init__(self):
        # key = (chat_id, user_id), value = deque[FragmentEntry]
        self._buffers: dict[
            tuple[int, int], deque[FragmentEntry],
        ] = defaultdict(lambda: deque(maxlen=self.MAX_FRAGMENTS))

    def add(
        self, *,
        chat_id: int, user_id: int,
        message_id: str, text: str,
    ) -> None:
        """Добавляет сообщение в буфер этого юзера."""
        key = (chat_id, user_id)
        self._buffers[key].append(FragmentEntry(
            message_id=message_id, text=text, received_at=time.time(),
        ))

    def detect_in_recent(
        self, *, chat_id: int, user_id: int,
    ) -> list[str] | None:
        """Проверяет: есть ли мат в склейке, которого нет в отдельных частях.

        Возвращает список mid всех частей если троллинг обнаружен; None если нет.

        Логика:
        1) Чистим протухшие фрагменты (>WINDOW_SECONDS).
        2) Если фрагментов <2 — точно не split-bypass.
        3) Склейка всех текстов с пробелами.
        4) Mat в склейке есть?
        5) Mat в каждом отдельном фрагменте?
           - Да в каждом → это просто матерящийся юзер, обычная модерация уже сработала
           - Нет ни в каком → ЭТО SPLIT-BYPASS, возвращаем все mid
           - В части есть, в части нет → mid тех в которых нет (это «прикрытие»)
        """
        key = (chat_id, user_id)
        buf = self._buffers.get(key)
        if not buf:
            return None

        cutoff = time.time() - self.WINDOW_SECONDS
        # Чистим протухшие
        while buf and buf[0].received_at < cutoff:
            buf.popleft()

        if len(buf) < 2:
            return None

        joined = " ".join(e.text for e in buf)
        if not _has_profanity(joined):
            return None

        # Mat в склейке есть. Проверяем — был ли он в каждом отдельном.
        any_clean = False
        for entry in buf:
            if not _has_profanity(entry.text):
                any_clean = True
                break

        if not any_clean:
            # Каждое отдельное сообщение содержит мат — это обычная агрессия,
            # нормальная модерация уже сработает на каждом по отдельности.
            return None

        # Это split-bypass: склейка содержит мат, но какие-то отдельные части
        # сами по себе чистые. Возвращаем все mid в окне для удаления.
        log.warning(
            "fragment_troll.detected",
            chat_id=chat_id, user_id=user_id,
            fragments=len(buf),
            joined_preview=joined[:100],
        )
        return [e.message_id for e in buf]

    def clear(self, *, chat_id: int, user_id: int) -> None:
        """Чистит буфер юзера (после удаления — чтобы не сработать повторно)."""
        self._buffers.pop((chat_id, user_id), None)
