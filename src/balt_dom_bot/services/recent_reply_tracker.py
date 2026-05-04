"""Анти-дубликат: бот не отвечает дважды на близкую тему за короткое время.

Проблема: жильцы пишут «Что с электричеством?» → бот отвечает шаблоном.
Через 30 секунд другой пишет «Отопления у всех нет?» → бот снова отвечает
тем же шаблоном «обращение принято, передано специалистам». Это спам в чат
от УК — раздражает остальных жильцов.

Решение: для каждого чата храним последние ответы бота с темой/тегами.
Перед отправкой нового ответа проверяем: «есть ли близкий ответ за последние
N минут?» → если да, silent (управляющий уже получил эскалацию, дублировать
ответ в чате не нужно).

«Близкий» = пересечение по теме (Theme) ИЛИ по ключевым словам (50%+ overlap).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Theme

log = get_logger(__name__)


@dataclass(frozen=True)
class RecentReply:
    theme: Theme
    keywords: frozenset[str]  # ключевые токены ответа
    sent_at: float


class RecentReplyTracker:
    """In-memory кольцо последних ответов бота по чатам."""

    MAX_PER_CHAT = 6
    DEDUP_WINDOW_SECONDS = 5 * 60  # 5 минут — окно дедупа

    def __init__(self):
        self._chats: dict[int, deque[RecentReply]] = {}

    def _cleanup(self, chat_id: int) -> None:
        buf = self._chats.get(chat_id)
        if not buf:
            return
        cutoff = time.time() - self.DEDUP_WINDOW_SECONDS
        while buf and buf[0].sent_at < cutoff:
            buf.popleft()

    def is_recent_duplicate(
        self, *,
        chat_id: int,
        theme: Theme,
        text: str,
    ) -> bool:
        """Возвращает True, если бот недавно отвечал на близкую тему.

        Признаки близости:
        1) Та же Theme (UTILITY → UTILITY): почти всегда дубликат темы.
           Исключение — Theme.OTHER не считается дубликатом (слишком общая).
        2) Большое пересечение ключевых слов вопроса.
        """
        self._cleanup(chat_id)
        buf = self._chats.get(chat_id)
        if not buf:
            return False

        new_kw = _extract_keywords(text)

        for prev in buf:
            # Тема: для конкретных тем считаем дубликатом «на ту же тему».
            if (
                theme != Theme.OTHER
                and theme == prev.theme
            ):
                log.info(
                    "recent_reply.duplicate_by_theme",
                    chat_id=chat_id, theme=theme.value,
                    age_s=round(time.time() - prev.sent_at),
                )
                return True
            # Keyword overlap: 2+ общих значимых слова — высокая близость
            # для коротких сообщений в чате УК.
            if new_kw and prev.keywords:
                inter = new_kw & prev.keywords
                if len(inter) >= 2:
                    log.info(
                        "recent_reply.duplicate_by_keywords",
                        chat_id=chat_id, overlap=list(inter)[:5],
                        overlap_count=len(inter),
                    )
                    return True
        return False

    def register(
        self, *,
        chat_id: int,
        theme: Theme,
        text: str,
    ) -> None:
        """Запоминает что бот только что ответил на тему `theme` про `text`."""
        buf = self._chats.setdefault(
            chat_id, deque(maxlen=self.MAX_PER_CHAT),
        )
        buf.append(RecentReply(
            theme=theme,
            keywords=_extract_keywords(text),
            sent_at=time.time(),
        ))


# Стоп-слова — мусорные токены, которые в любом сообщении.
_STOPWORDS = frozenset({
    "это", "так", "что", "как", "вот", "там", "тут", "уже", "ещё", "еще",
    "его", "их", "ему", "себя", "она", "они", "оно", "вас", "нас", "наш",
    "ваш", "наша", "наше", "ваша", "ваше", "наши", "ваши",
    "не", "ни", "но", "да", "и", "или", "а", "ну", "ой", "ой-ой", "ох",
    "по", "на", "в", "с", "со", "у", "от", "для", "до", "из",
    "когда", "где", "куда", "зачем", "почему", "ли", "же", "ведь",
    "быть", "был", "была", "было", "были", "есть", "будет", "будут",
    "очень", "опять", "снова", "уже", "всё", "все", "весь", "вся",
    "почему", "почему-то", "просто", "только", "также",
    "пожалуйста", "спасибо", "здравствуйте", "добрый", "доброе", "вечер",
    "утро", "день", "ночи", "привет",
})


def _extract_keywords(text: str) -> frozenset[str]:
    """Извлекает значимые токены из текста для сравнения близости.

    Простая реализация: нижний регистр, токены ≥4 символов, без стоп-слов.
    Эта эвристика достаточна — мы не пытаемся понять смысл, а ищем повторы.
    """
    if not text:
        return frozenset()
    text_lc = text.lower()
    # Нормализуем разделители — режем по любым не-буквам.
    import re
    tokens = re.findall(r"[а-яёa-z]+", text_lc)
    significant = {
        t for t in tokens
        if len(t) >= 4 and t not in _STOPWORDS
    }
    return frozenset(significant)
