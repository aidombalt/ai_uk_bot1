"""Anti-flood: rate-limiter и дедуп эскалаций.

Хранит данные в памяти процесса. После рестарта счётчики сбрасываются —
это OK, потому что cooldown-окна короткие (минуты).

Используется в Pipeline:
* `should_silence_reply(chat_id, user_id)` — вернёт True, если пользователь
  превысил лимит ответов и должен молчаливо игнорироваться.
* `register_reply(chat_id, user_id)` — отметить, что бот ответил.
* `should_dedupe_escalation(chat_id, user_id)` — True если эскалацию по
  этому юзеру за последние N минут уже создавали.
* `register_escalation(chat_id, user_id)` — отметить.

Причина двух раздельных счётчиков: cooldown по ответам отвечает за публичную
картину в чате (бот не должен «глупо» спамить), а дедуп эскалаций — за то,
чтобы управляющий не получал 100 уведомлений от одного тролля.
"""

from __future__ import annotations

import time
from collections import deque

from balt_dom_bot.log import get_logger

log = get_logger(__name__)


class CooldownManager:
    def __init__(
        self,
        *,
        replies_per_minute: int = 2,
        cooldown_minutes: int = 5,
        escalation_dedup_minutes: int = 10,
        trolling_window_minutes: int = 5,
    ) -> None:
        self._replies: dict[tuple[int, int], deque[float]] = {}
        self._escalations: dict[tuple[int, int], float] = {}
        # Счётчик off-topic сообщений с упоминанием бота (троллинг-детектор).
        # Отдельный от обычного cooldown — здесь нам важно НЕ время с последнего
        # ответа, а сколько раз юзер тегнул бота, не получая ответа.
        self._trolling: dict[tuple[int, int], deque[float]] = {}
        self._replies_per_minute = replies_per_minute
        self._cooldown_seconds = cooldown_minutes * 60
        self._escalation_dedup_seconds = escalation_dedup_minutes * 60
        self._trolling_window_seconds = trolling_window_minutes * 60

    def _now(self) -> float:
        return time.time()

    def _prune_replies(self, key: tuple[int, int], now: float) -> deque[float]:
        """Чистит таймстемпы старше окна (cooldown_seconds)."""
        dq = self._replies.setdefault(key, deque(maxlen=20))
        threshold = now - self._cooldown_seconds
        while dq and dq[0] < threshold:
            dq.popleft()
        return dq

    def should_silence_reply(self, *, chat_id: int, user_id: int | None) -> bool:
        """Если True — бот не должен отвечать (юзер в cooldown).

        Логика: смотрим сколько раз бот отвечал этому юзеру за последнюю минуту.
        Если ≥ replies_per_minute → cooldown активен на cooldown_minutes от
        последнего ответа.
        """
        if user_id is None:
            return False
        now = self._now()
        key = (chat_id, user_id)
        dq = self._prune_replies(key, now)
        if not dq:
            return False
        # Считаем ответы за последнюю минуту
        last_minute = now - 60
        recent = sum(1 for t in dq if t >= last_minute)
        if recent >= self._replies_per_minute:
            log.info(
                "cooldown.silence_reply",
                chat_id=chat_id, user_id=user_id,
                recent_replies=recent, since_last=int(now - dq[-1]),
            )
            return True
        return False

    def register_reply(self, *, chat_id: int, user_id: int | None) -> None:
        if user_id is None:
            return
        now = self._now()
        key = (chat_id, user_id)
        dq = self._prune_replies(key, now)
        dq.append(now)

    def should_dedupe_escalation(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        severity: str = "normal",
    ) -> bool:
        """Дедуп эскалации.

        severity='critical' — НЕ дедуплим. Используется для AGGRESSION,
        PROVOCATION, SPAM — каждое такое событие должно дойти до управляющего,
        чтобы он мог решать о бане. Защита от флуда обеспечивается отдельно
        через strike-counter в Moderator (после N страйков — авто-бан).

        severity='normal' — дедуплим. Для low_confidence / llm_error /
        обычных обращений: если уже эскалировали по этому юзеру, не шлём
        повторно в течение dedup-окна (10 минут).
        """
        if severity == "critical":
            return False
        if user_id is None:
            return False
        now = self._now()
        last = self._escalations.get((chat_id, user_id))
        if last is None:
            return False
        if now - last < self._escalation_dedup_seconds:
            log.info(
                "cooldown.dedup_escalation",
                chat_id=chat_id, user_id=user_id,
                since_last=int(now - last), severity=severity,
            )
            return True
        return False

    def register_escalation(self, *, chat_id: int, user_id: int | None) -> None:
        if user_id is None:
            return
        self._escalations[(chat_id, user_id)] = self._now()

    def register_offtopic_mention(
        self, *, chat_id: int, user_id: int | None,
    ) -> int:
        """Регистрирует факт «юзер тегнул бота, но классификатор сказал
        off-topic». Возвращает счётчик таких событий в окне (по умолчанию 5 мин).

        Используется для anti-trolling в Pipeline:
        - count == 1 → silent (мог не понять, не наказываем)
        - count >= 2 → удаляем сообщение
        - count >= 3 → удаляем + strike (как при агрессии)
        - после strikes_for_ban → kick через Moderator
        """
        if user_id is None:
            return 0
        now = self._now()
        key = (chat_id, user_id)
        dq = self._trolling.setdefault(key, deque(maxlen=20))
        threshold = now - self._trolling_window_seconds
        while dq and dq[0] < threshold:
            dq.popleft()
        dq.append(now)
        return len(dq)

    # --- утилиты для отладки/тестов -----------------------------------------

    def reset(self) -> None:
        """Сброс всех счётчиков (для тестов или ручного сброса в GUI)."""
        self._replies.clear()
        self._escalations.clear()
        log.info("cooldown.reset")
