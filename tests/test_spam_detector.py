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

    def test_latin_k_to_cyrillic_k(self) -> None:
        # д0ставkу → доставку (Latin k → к; digit 0 между кириллицей → о)
        assert "доставку" in _normalize_obfuscated("д0ставkу").lower()

    def test_digit_zero_to_o_between_cyrillic(self) -> None:
        # д0ставка → доставка (0 между кириллическими символами → о)
        assert "доставка" in _normalize_obfuscated("д0ставка").lower()

    def test_digit_zero_preserved_in_numbers(self) -> None:
        # 100к/неделю — цифры не должны переводиться в буквы
        result = _normalize_obfuscated("100к/неделю")
        assert "100к" in result, f"Число 100 не должно изменяться, получено: {result!r}"

    def test_vodoschetchiков_obfuscation(self) -> None:
        # В0Д0СЧЕТЧИК0В (0 как буква о между кириллицей) → ВОДОСЧЕТЧИКОВ
        result = _normalize_obfuscated("В0Д0СЧЕТЧИК0В").lower()
        assert "водосчетчик" in result

    def test_euro_sign_to_e(self) -> None:
        # Тр€буются → Требуются
        assert "требуются" in _normalize_obfuscated("Тр€буются").lower()

    def test_full_new_spam_normalized(self) -> None:
        # Полное сообщение из логов 2026-05-12 (второй паттерн)
        spam = "Тр€буются p@ботниkи н@ д0ставkу! 3П от 15Ок в мecяц. Бeз oпытa."
        result = _normalize_obfuscated(spam).lower()
        assert "доставк" in result   # д0ставkу → доставку
        assert "требу" in result     # Тр€буются → Требуются
        assert "без опыта" in result # Бeз oпытa → Без опыта


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

    def test_income_pattern_15ok_month(self) -> None:
        """15Ок в мecяц + @handle — income-паттерн с O=ноль."""
        verdict = detect("ЗП от 15Ок в мecяц, пиши @rabota_dostavka")
        assert verdict.is_spam
        assert verdict.category == "earn"

    def test_income_100k_week_with_mention(self) -> None:
        """100к/неделю + @mention → detect ловит без LLM."""
        verdict = detect("Доход 100к/неделю, пиши @work_bot")
        assert verdict.is_spam
        assert verdict.category == "earn"


class TestNewSpamPatterns:
    """Реальный спам из логов 2026-05-12."""

    OBFUSC_SPAM_TEXT = (
        "Тр€буются p@ботниkи н@ д0ставkу! "
        "3П от 15Ок в мecяц. Бeз oпытa. "
        "Пиcaть в тг @rabota_dostavka"
    )
    WATER_METER_SPAM = (
        "Установка В0Д0СЧЕТЧИК0В! Акция до конца недели. "
        "Пломбировка в подарок. Пишите в ЛС или звоните +7(999)123-45-67"
    )
    COURIER_PHONE_SPAM = (
        "Тpебyются кyрьеры!!! 3п от 100k в день. "
        "Свободный гpaфик, бeз опытa. "
        "Писать в ЛС -> @manager_tg"
    )

    def test_obfusc_spam_is_spam(self) -> None:
        verdict = detect(self.OBFUSC_SPAM_TEXT)
        assert verdict.is_spam, (
            f"category={verdict.category}, matched={verdict.matched}"
        )

    def test_obfusc_spam_category_earn(self) -> None:
        assert detect(self.OBFUSC_SPAM_TEXT).category == "earn"

    def test_obfusc_spam_candidate_triggers(self) -> None:
        assert is_spam_candidate(self.OBFUSC_SPAM_TEXT)

    def test_water_meter_candidate_triggers(self) -> None:
        """Реклама водосчётчиков с телефоном → LLM-кандидат."""
        assert is_spam_candidate(self.WATER_METER_SPAM), (
            "Реклама с телефоном + В0Д (script mix) должна быть кандидатом"
        )

    def test_courier_phone_spam_candidate(self) -> None:
        """Курьерский спам с @handle → LLM-кандидат."""
        assert is_spam_candidate(self.COURIER_PHONE_SPAM)

    def test_100k_in_numbers_not_corrupted(self) -> None:
        """Цифра 0 в числах должна сохраняться, не превращаясь в букву о."""
        norm = _normalize_obfuscated("100к/неделю")
        assert "100к" in norm, f"Число 100 не должно изменяться: {norm!r}"

    def test_ls_income_detected_directly(self) -> None:
        """«100k в неделю» + «Пиши в лс» → rule 8 ловит без @mention."""
        verdict = detect(
            "Ищешь работу? Свободный график, з/п от 100k в неделю. Без опыта. Пиши в лс!"
        )
        assert verdict.is_spam
        assert verdict.category == "earn"

    def test_drug_emoji_pair_with_context_detected(self) -> None:
        """❄️+🍁 + 'доставка' → rule 2b → drugs."""
        verdict = detect("Только ❄️ и 🍁. Доставка по городу. Оплата криптой @HR_best_work")
        assert verdict.is_spam
        assert verdict.category == "drugs"

    def test_drug_emoji_pair_with_income_and_ls_detected(self) -> None:
        """Точный текст со скриншота: обфускация + ❄️🍁 + «в лс» → детектируется."""
        text = (
            "«Ищeшь pa6oту? 💸 Интepecные квecты по гopoду! "
            "Cвoбодный гpaфик, з/п от 100k в неделю. Без опыта. Пиши в лс! ❄️🍁🔥»"
        )
        verdict = detect(text)
        assert verdict.is_spam, f"category={verdict.category}, matched={verdict.matched}"

    def test_drug_emoji_alone_no_context_not_spam(self) -> None:
        """❄️+🍁 без коммерческого контекста → не спам."""
        verdict = detect("❄️ зима пришла, 🍁 осень прошла, всем тепла!")
        assert not verdict.is_spam

    def test_ls_candidate_triggers(self) -> None:
        """«Пиши в лс» + «доход»/«без опыта» → is_spam_candidate True."""
        assert is_spam_candidate(
            "з/п от 100к в неделю, без опыта, пиши в лс"
        )

    def test_ls_alone_without_commercial_not_candidate(self) -> None:
        """«В лс» без коммерческих слов → не кандидат."""
        assert not is_spam_candidate("Кто нашёл ключи — напишите в лс, очень нужно")


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
        # Числа с нулями — не должны ловиться как наркотики / income
        "Тариф повысили на 6% с января",
        "В доме 6 подъездов, 120 квартир",
        # Числа 3-значные без к/неделю/месяц — не income pattern
        "Счёт за ЖКХ 2800 рублей в этом месяце",
        # @упоминание без коммерческих слов — не кандидат
        "Напоминаю про субботник @all в 10:00!",
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
