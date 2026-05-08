"""Smoke-тесты CooldownManager.

Проверяем:
1. Первые N ответов не вызывают cooldown.
2. N+1 ответ в минуту → cooldown активируется.
3. После активации cooldown держится cooldown_minutes (не сбрасывается через 60с).
4. По истечении cooldown_minutes ответы снова разрешены.
5. reset() снимает cooldown.
6. Разные (chat_id, user_id) изолированы.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from balt_dom_bot.services.cooldown import CooldownManager


CHAT_ID = -100_000_001
USER_ID = 42


def _make_cm(
    *,
    replies_per_minute: int = 2,
    cooldown_minutes: int = 5,
) -> CooldownManager:
    return CooldownManager(
        replies_per_minute=replies_per_minute,
        cooldown_minutes=cooldown_minutes,
        escalation_dedup_minutes=10,
    )


def test_no_cooldown_before_threshold() -> None:
    """До лимита ответов cooldown не срабатывает."""
    cm = _make_cm()
    assert not cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    assert not cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)


def test_cooldown_triggers_at_threshold() -> None:
    """Два ответа в минуту → третий вызов should_silence_reply возвращает True."""
    cm = _make_cm(replies_per_minute=2)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)


def test_cooldown_persists_after_60_seconds() -> None:
    """После активации cooldown держится cooldown_minutes, не сбрасываться через 60с.

    Ключевой сценарий (реальный баг):
    - Два ответа в t=0
    - Сообщение 3 в t=30 → активирует cooldown (оба ответа в 60с-окне)
    - Сообщение 4 в t=61 → ответы вышли из 60с-окна, но cooldown ещё активен
    """
    cm = _make_cm(replies_per_minute=2, cooldown_minutes=5)

    t0 = 0.0
    with patch.object(cm, "_now", return_value=t0):
        cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
        cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)

    # t0+30: оба ответа в 60-секундном окне → активируем cooldown
    with patch.object(cm, "_now", return_value=t0 + 30):
        assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID), (
            "При 2 ответах за 60с должен активироваться cooldown"
        )

    # t0+61: ответы вышли из 60с-окна, но cooldown_until ещё не истёк
    with patch.object(cm, "_now", return_value=t0 + 61):
        assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID), (
            "Cooldown должен держаться 5 минут, не сбрасываться через 61 секунду"
        )


def test_cooldown_expires_after_cooldown_minutes() -> None:
    """По истечении cooldown_minutes ответы снова разрешены."""
    cm = _make_cm(replies_per_minute=2, cooldown_minutes=5)

    t0 = time.time()
    with patch.object(cm, "_now", return_value=t0):
        cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
        cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
        # Trigger the cooldown
        assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)

    # After 5 minutes + 1 second
    with patch.object(cm, "_now", return_value=t0 + 5 * 60 + 1):
        assert not cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID), (
            "Через 5+ минут cooldown должен истечь"
        )


def test_cooldown_reset_clears_silence() -> None:
    """reset() снимает активный cooldown."""
    cm = _make_cm(replies_per_minute=2)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)
    cm.reset()
    assert not cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)


def test_cooldown_isolated_per_user() -> None:
    """Cooldown одного пользователя не влияет на другого в том же чате."""
    cm = _make_cm(replies_per_minute=2)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)

    other_user = USER_ID + 1
    assert not cm.should_silence_reply(chat_id=CHAT_ID, user_id=other_user)


def test_cooldown_scenario_from_real_case() -> None:
    """Реальный кейс: бот ответил дважды за 30с, потом 5 сообщений подряд.

    Сообщения 3-5 должны быть подавлены cooldown'ом (не только первые 60с).
    """
    cm = _make_cm(replies_per_minute=2, cooldown_minutes=5)
    t0 = 0.0

    # Ответ 1 (t=0) и ответ 2 (t=24): два ответа за минуту
    with patch.object(cm, "_now", return_value=t0):
        cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)
    with patch.object(cm, "_now", return_value=t0 + 24):
        cm.register_reply(chat_id=CHAT_ID, user_id=USER_ID)

    # Сообщение 3 (t=33): cooldown активируется
    with patch.object(cm, "_now", return_value=t0 + 33):
        assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID)

    # Сообщение 4 (t=83): 83 секунды с ответа 1, 59с с ответа 2 → cooldown ещё активен
    with patch.object(cm, "_now", return_value=t0 + 83):
        assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID), (
            "Сообщение 4 (через 83с) должно подавляться: cooldown ещё активен 5 минут"
        )

    # Сообщение 5 (t=122): 122с с ответа 1, оба вышли из 60с-окна → НО cooldown ещё активен
    with patch.object(cm, "_now", return_value=t0 + 122):
        assert cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID), (
            "Сообщение 5 (через 122с = 'номер гендиректора') должно подавляться: "
            "cooldown активен до t0+33+300=333с"
        )

    # После 5 минут с момента активации cooldown'а (t0+33+300=333с)
    with patch.object(cm, "_now", return_value=t0 + 334):
        assert not cm.should_silence_reply(chat_id=CHAT_ID, user_id=USER_ID), (
            "Через 5+ минут с активации cooldown должен истечь"
        )
