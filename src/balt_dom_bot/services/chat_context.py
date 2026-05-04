"""Буфер последних сообщений в чате — контекст для классификатора.

Цель: дать LLM понимание «что сейчас обсуждается в чате», чтобы:
* Распознать продолжение треда («да, козлы» в контексте «надо менять УК»)
* Не отвечать на повторы того же вопроса от разных жильцов
* Видеть эмоциональный фон чата

Хранение: in-memory кольцевой буфер per-chat. Не БД — это летучий контекст,
после рестарта начинаем заново. Это ОК — мы хотим контекст «прямо сейчас»,
не «исторический».

Параметры:
* MAX_PER_CHAT — сколько сообщений хранить (8)
* MAX_AGE_SECONDS — старше этого выбрасываем (10 минут)

Чтобы избежать утечек памяти, при каждом обращении чистим протухшие сообщения.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from balt_dom_bot.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ChatContextEntry:
    user_id: int | None
    user_name: str | None
    text: str
    bot_reply: str | None  # если бот ответил на это сообщение — какой текст
    received_at: float


class ChatContextManager:
    """In-memory буфер последних сообщений по каждому чату."""

    MAX_PER_CHAT = 8
    MAX_AGE_SECONDS = 10 * 60  # 10 минут

    def __init__(self):
        self._buffers: dict[int, deque[ChatContextEntry]] = {}

    def add(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        user_name: str | None,
        text: str,
        bot_reply: str | None = None,
    ) -> None:
        """Добавляет сообщение жильца (и опционально ответ бота на него)."""
        buf = self._buffers.setdefault(
            chat_id, deque(maxlen=self.MAX_PER_CHAT),
        )
        buf.append(ChatContextEntry(
            user_id=user_id, user_name=user_name,
            text=text[:300],  # обрезаем длинные сообщения чтобы не раздуть промпт
            bot_reply=bot_reply[:200] if bot_reply else None,
            received_at=time.time(),
        ))

    def attach_bot_reply(
        self, *, chat_id: int, user_id: int | None, reply_text: str,
    ) -> None:
        """Дописывает ответ бота к последнему сообщению этого юзера в чате.

        Используется ПОСЛЕ отправки ответа — чтобы анти-дубликат знал
        о чём бот уже говорил.
        """
        buf = self._buffers.get(chat_id)
        if not buf:
            return
        # Идём с конца — ищем последнее сообщение этого юзера без bot_reply.
        for i in range(len(buf) - 1, -1, -1):
            entry = buf[i]
            if entry.user_id == user_id and entry.bot_reply is None:
                buf[i] = ChatContextEntry(
                    user_id=entry.user_id, user_name=entry.user_name,
                    text=entry.text, bot_reply=reply_text[:200],
                    received_at=entry.received_at,
                )
                return

    def get_context(
        self, *, chat_id: int, exclude_last: bool = True,
    ) -> list[ChatContextEntry]:
        """Возвращает свежие сообщения чата для контекста.

        exclude_last=True — выкидывает самое последнее сообщение (это то,
        которое сейчас классифицируется — оно бы только запутало LLM).
        Старые (>MAX_AGE) выбрасываем при чтении.
        """
        buf = self._buffers.get(chat_id)
        if not buf:
            return []
        cutoff = time.time() - self.MAX_AGE_SECONDS
        # Чистим протухшие, начиная с головы.
        while buf and buf[0].received_at < cutoff:
            buf.popleft()
        if not buf:
            return []
        items = list(buf)
        if exclude_last and items:
            items = items[:-1]
        return items

    def clear(self, chat_id: int) -> None:
        self._buffers.pop(chat_id, None)
