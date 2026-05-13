"""Тесты для FragmentTrollDetector и _has_profanity."""
import time
import pytest

from balt_dom_bot.services.fragment_troll import (
    FragmentTrollDetector,
    _has_profanity,
)


# ---------------------------------------------------------------------------
# _has_profanity — корректность распознавания
# ---------------------------------------------------------------------------

class TestHasProfanity:
    def test_standalone_mat(self):
        assert _has_profanity("бля") is True

    def test_blyad(self):
        assert _has_profanity("блядь") is True

    def test_huy(self):
        assert _has_profanity("хуй") is True

    def test_huyovaya_compact(self):
        # split-bypass: "ху" + "ёвая" склеивается в "хуёвая"
        assert _has_profanity("хуёвая") is True

    def test_oskorblyaet_false_positive(self):
        # «оскорбляет» содержит «бля» как часть корня — НЕ мат.
        assert _has_profanity("оскорбляет") is False

    def test_izobrazhenie_false_positive(self):
        assert _has_profanity("изображение") is False

    def test_clean_text(self):
        assert _has_profanity("Здравствуйте! Лифт сломан.") is False

    def test_emoji_only(self):
        assert _has_profanity("🤬🤬🤬") is False

    def test_court_threat(self):
        # «Я пойду в суд» — не мат.
        assert _has_profanity("Я пойду в суд") is False

    def test_uk_oskorblyaet(self):
        assert _has_profanity("Ук меня оскорбляет!! Я пойду в суд") is False

    def test_mixed_sentence_with_mat(self):
        assert _has_profanity("вы все пиздец какие нехорошие") is True

    def test_split_no_space(self):
        # Слитная версия — compact-обнаружение split-bypass.
        assert _has_profanity("ху ёвая") is True  # joined compact "хуёвая"


# ---------------------------------------------------------------------------
# FragmentTrollDetector — сценарии обнаружения
# ---------------------------------------------------------------------------

def _add(detector, *, chat_id=1, user_id=1, message_id, text, dt=0):
    """Вспомогательная функция: добавляет сообщение в буфер."""
    entry = detector._buffers[(chat_id, user_id)]  # defaultdict
    from balt_dom_bot.services.fragment_troll import FragmentEntry
    entry.append(FragmentEntry(
        message_id=message_id, text=text,
        received_at=time.time() - dt,
    ))


class TestFragmentTrollDetector:

    def test_genuine_split_bypass_detected(self):
        """ху + ёвая → split-bypass, возвращает оба mid."""
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1", text="ху")
        d.add(chat_id=1, user_id=1, message_id="m2", text="ёвая")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result == ["m1", "m2"]

    def test_single_message_no_detection(self):
        """Одно сообщение — детектор молчит."""
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1", text="ху")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result is None

    def test_clean_messages_no_detection(self):
        """Чистые сообщения — нет срабатывания."""
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1", text="Лифт сломан")
        d.add(chat_id=1, user_id=1, message_id="m2", text="Прошу починить")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result is None

    def test_individual_mat_not_fragment_bypass(self):
        """Если последнее сообщение само по себе матерное — не split-bypass."""
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1", text="нормально")
        d.add(chat_id=1, user_id=1, message_id="m2", text="хуйня полная")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        # Нормальная модерация обработает m2, fragment_troll не должен срабатывать.
        assert result is None

    def test_legitimate_complaint_then_offensive_not_deleted(self):
        """Жалоба жильца + агрессивные эмодзи + «оскорбляет» → ничего не удаляется.

        Воспроизводит баг из продакшена:
        - msg1: легитимная жалоба
        - msg2: 🤬🤬🤬 (нет мата в смысле паттернов)
        - msg3: «Ук меня оскорбляет!! Я пойду в суд»
        Бот не должен удалять ни одно сообщение.
        """
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1",
              text="Здравствуйте! 1 подъезд, средний лифт требует уборки")
        d.add(chat_id=1, user_id=1, message_id="m2", text="🤬🤬🤬")
        d.add(chat_id=1, user_id=1, message_id="m3",
              text="Ук меня оскорбляет!!  Я пойду в суд")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result is None

    def test_minimum_suffix_only_recent_deleted(self):
        """Старое легитимное сообщение НЕ удаляется при split-bypass в свежих.

        msg1 (старый): легитимный
        msg2 + msg3 (свежие): вместе образуют мат («ху» + «ёвая»)
        → должны удалиться только msg2 и msg3.
        """
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1",
              text="Добрый день, у нас не работает домофон")
        d.add(chat_id=1, user_id=1, message_id="m2", text="ху")
        d.add(chat_id=1, user_id=1, message_id="m3", text="ёвая")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result == ["m2", "m3"]
        assert "m1" not in result

    def test_expired_fragments_cleaned(self):
        """Протухшие фрагменты (> WINDOW_SECONDS) удаляются из буфера."""
        d = FragmentTrollDetector()
        _add(d, message_id="old", text="ху", dt=d.WINDOW_SECONDS + 5)
        d.add(chat_id=1, user_id=1, message_id="fresh", text="ёвая")
        # Старый фрагмент протух → только 1 свежий → не хватает для bypass.
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result is None

    def test_clear_resets_buffer(self):
        """После clear буфер пуст — повторное сообщение не триггерит."""
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="m1", text="ху")
        d.add(chat_id=1, user_id=1, message_id="m2", text="ёвая")
        d.clear(chat_id=1, user_id=1)
        # После clear буфер сброшен, добавляем ещё одно — нет пары.
        d.add(chat_id=1, user_id=1, message_id="m3", text="ёвая")
        result = d.detect_in_recent(chat_id=1, user_id=1)
        assert result is None

    def test_different_users_isolated(self):
        """Буферы разных пользователей не пересекаются."""
        d = FragmentTrollDetector()
        d.add(chat_id=1, user_id=1, message_id="u1m1", text="ху")
        d.add(chat_id=1, user_id=2, message_id="u2m1", text="ёвая")
        # user_id=1 имеет только одно сообщение → нет bypass.
        assert d.detect_in_recent(chat_id=1, user_id=1) is None
        # user_id=2 тоже только одно сообщение.
        assert d.detect_in_recent(chat_id=1, user_id=2) is None
