"""Smoke-тесты: детектор спама/наркотиков в жильцовских чатах.

Проверяем:
* Реальный обфусцированный текст из логов (2026-05-12)
* Различные техники обфускации: гомоглифы, emoji-границы, пробелы в словах
* Нормализацию: _normalize_obfuscated
* Ложные срабатывания: легитимные сообщения жильцов
"""

import pytest

from balt_dom_bot.services.spam_detector import (
    SpamVerdict,
    _normalize_obfuscated,
    detect,
    is_spam_candidate,
)


# ---------------------------------------------------------------------------
# Нормализация текста
# ---------------------------------------------------------------------------

class TestNormalizeObfuscated:
    def test_latin_homoglyphs_to_cyrillic(self) -> None:
        # Кpиcтaллы: p,c,a — латинские; после нормализации → Кристаллы
        assert "кристаллы" in _normalize_obfuscated("Кpиcтaллы").lower()

    def test_u_to_cyrillic_i(self) -> None:
        # шишкu → шишки (u Latin → и Cyrillic)
        assert "шишки" in _normalize_obfuscated("шишкu").lower()

    def test_digit_6_to_b(self) -> None:
        # 6 → б в контексте слова
        result = _normalize_obfuscated("p a 6 o т ы").lower()
        assert "б" in result

    def test_emoji_replaced_with_space(self) -> None:
        result = _normalize_obfuscated("🍁шишки🍁")
        # emoji заменены пробелами → слово не слипается с посторонними символами
        assert "шишки" in result
        assert "🍁" not in result

    def test_spaced_word_collapsed(self) -> None:
        # 4+ одиночных символа через пробел → слово
        result = _normalize_obfuscated("р а б о т ы").lower()
        assert "работы" in result

    def test_spaced_word_not_collapsed_on_short_seq(self) -> None:
        # Последовательность из 3 символов НЕ должна склеиваться ({3,} означает ≥4)
        result = _normalize_obfuscated("а б в")
        # три одиночных символа остаются как есть
        assert result == "а б в"

    def test_full_spam_sample_normalized(self) -> None:
        spam = (
            "🔥 Ищeм aктивныx людeй для p a 6 o т ы 🔥 "
            "📦 Дocтaвкa кyрьepoм. ❄️Кpиcтaллы❄️, 🍁шишкu🍁."
        )
        result = _normalize_obfuscated(spam).lower()
        assert "кристаллы" in result
        assert "шишки" in result


# ---------------------------------------------------------------------------
# Реальный спам из логов (2026-05-12)
# ---------------------------------------------------------------------------

class TestRealSpamFromLogs:
    SPAM_TEXT = (
        "🔥 Ищeм aктивныx людeй для p a 6 o т ы 🔥 "
        "📦 Дocтaвкa кyрьepoм. ❄️Кpиcтaллы❄️, 🍁шишкu🍁. "
        "Дoxoд oт 15O к. в нeдeлю! Oплaтa кaждый дeнь. "
        "Пиши в лc @mega_rabota_24"
    )

    def test_is_spam(self) -> None:
        verdict = detect(self.SPAM_TEXT)
        assert verdict.is_spam, "Реальный спам из логов должен быть обнаружен"

    def test_category_drugs(self) -> None:
        verdict = detect(self.SPAM_TEXT)
        assert verdict.category == "drugs", (
            f"Ожидается категория 'drugs', получено '{verdict.category}'. "
            f"Маркеры: {verdict.matched}"
        )

    def test_high_confidence(self) -> None:
        verdict = detect(self.SPAM_TEXT)
        assert verdict.confidence >= 0.9

    def test_matched_evidence_present(self) -> None:
        verdict = detect(self.SPAM_TEXT)
        assert verdict.matched, "Список сработавших маркеров не должен быть пустым"


# ---------------------------------------------------------------------------
# Техники обфускации по отдельности
# ---------------------------------------------------------------------------

class TestObfuscationTechniques:
    def test_homoglyph_crystals(self) -> None:
        """Кpиcтaллы (p,c,a — латинские) → обнаруживается как drugs."""
        verdict = detect("❄️Кpиcтaллы❄️ доставка по городу пишите в лс")
        assert verdict.is_spam
        assert verdict.category == "drugs"

    def test_emoji_boundary_shishki(self) -> None:
        """🍁шишкu🍁 — emoji-граница + u→и обфускация."""
        verdict = detect("🍁шишкu🍁 свежие, пишите в личку @dealer_bot")
        assert verdict.is_spam
        assert verdict.category == "drugs"

    def test_spaced_out_word_in_drug_context(self) -> None:
        """Слово разбито пробелами, но контекст явно наркотический."""
        verdict = detect("шишки кристаллы и р а з н о е доставим")
        assert verdict.is_spam
        assert verdict.category == "drugs"

    def test_pure_latin_homoglyphs(self) -> None:
        """Только латинские гомоглифы без emoji."""
        verdict = detect("кpиcтaллы мефедрон купить")
        assert verdict.is_spam
        assert verdict.category == "drugs"

    def test_kristall_standalone(self) -> None:
        """'кристалл' без дополнительных слов — прямой drug-маркер."""
        verdict = detect("кристалл, закладки, доставка")
        assert verdict.is_spam
        assert verdict.category == "drugs"

    def test_oplate_kazhdyi_den_earn_signal(self) -> None:
        """'Оплата каждый день' + @mention → спам earn."""
        verdict = detect(
            "работа курьером, оплата каждый день, пиши @boss_work"
        )
        assert verdict.is_spam

    def test_daily_income_obfuscated(self) -> None:
        """Нормализованный текст с обфускацией «доход» и упоминанием."""
        # Дoxoд → доход после нормализации; earn marker "доход от 15"
        verdict = detect("Дoxoд oт 150 тысяч в неделю @work_now пишите нам")
        assert verdict.is_spam


# ---------------------------------------------------------------------------
# Ложные срабатывания: обычные сообщения жильцов
# ---------------------------------------------------------------------------

class TestFalsePositives:
    @pytest.mark.parametrize("text", [
        "Когда починят домофон в третьей парадной?",
        "Уборщица опять не убрала в подъезде, безобразие",
        "Добрый день, не работает лифт на 8 этаже",
        "Вчера видел соседа — говорит, что кладовка залита",
        "Когда будет собрание жильцов?",
        "Потерял ключи от машины, Toyota, кто нашёл — напишите",
        "Соседи, тихий час с 13 до 15, просьба соблюдать",
        # Слово «кристально» — НЕ должно ловиться (пишется с ь, не кристалл)
        "У нас кристально чистая вода из фильтра",
        # Слово «скорость» с пробелом-суффиксом — не должно ловиться
        "Скорость интернета в доме упала вдвое",
        # Обычное упоминание в чате без спама
        "Привет @all_residents, напоминаю про субботник в 10:00",
        # Числа с нулями — не должны ловиться как наркотики
        "Тариф повысили на 6% с января",
        "В доме 6 подъездов, 120 квартир",
    ])
    def test_legit_message_not_spam(self, text: str) -> None:
        verdict = detect(text)
        assert not verdict.is_spam, (
            f"Легитимное сообщение ложно определено как спам: {text!r}\n"
            f"category={verdict.category}, matched={verdict.matched}"
        )

    def test_whitelist_link_not_spam(self) -> None:
        """Ссылка на gosuslugi.ru — НЕ спам."""
        verdict = detect(
            "Подайте заявку на перерасчёт на gosuslugi.ru/gkh"
        )
        assert not verdict.is_spam

    def test_gov_link_with_crypto_word_not_spam(self) -> None:
        """Закон о криптовалюте + ссылка на pravo.gov.ru — НЕ спам."""
        verdict = detect(
            "Новый закон о криптовалюте принят: pravo.gov.ru/document/123"
        )
        assert not verdict.is_spam
