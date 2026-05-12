"""Smoke-тесты: LLM-детектор спама (SpamLLMChecker) и trigg-функция is_spam_candidate.

Тесты LLM-чекера используют мок YandexGptClient — не делают реальных API-запросов.
"""

import json

import pytest

from balt_dom_bot.services.spam_detector import is_spam_candidate
from balt_dom_bot.services.spam_llm_checker import SpamLLMVerdict, _parse_verdict


# ---------------------------------------------------------------------------
# is_spam_candidate(): триггер для LLM-проверки
# ---------------------------------------------------------------------------

class TestIsSpamCandidate:
    """is_spam_candidate должна ловить сообщения с @упоминанием + коммерческими словами."""

    def test_courier_job_spam_from_screenshot(self) -> None:
        text = (
            "Работa курьером! Свободный грaфик, оплaтa от 100к/неделя. "
            "Берем без опытa. Писать в ЛС 👉 @skam_bot"
        )
        assert is_spam_candidate(text), "Курьерская реклама с @handle должна быть кандидатом"

    def test_drug_courier_obfuscated(self) -> None:
        text = (
            "🔥 Ищeм aктивныx людeй для p a 6 o т ы 🔥 "
            "Дoxoд oт 150 к. в нeдeлю! @mega_rabota_24"
        )
        assert is_spam_candidate(text)

    def test_earn_with_income_pattern(self) -> None:
        assert is_spam_candidate("Доход 120к/неделю, пиши @boss_work")

    def test_work_offer_without_mention_not_candidate(self) -> None:
        # Нет @упоминания — не кандидат (дорого вызывать LLM на каждое слово «работа»)
        assert not is_spam_candidate("Ищу работу, кто знает вакансии рядом?")

    def test_normal_mention_no_commercial_words(self) -> None:
        assert not is_spam_candidate("Привет @Дима, ты слышал про субботник?")

    def test_lost_keys_with_mention(self) -> None:
        assert not is_spam_candidate("Кто нашёл ключи? @all_residents, помогите!")

    def test_all_residents_no_commercial(self) -> None:
        assert not is_spam_candidate("Напоминаю про субботник @all в 10:00!")

    def test_complaint_with_bot_mention(self) -> None:
        assert not is_spam_candidate("Лифт не работает, @primorskiy_drug_bot помоги!")

    def test_no_mention_at_all(self) -> None:
        assert not is_spam_candidate("Когда починят домофон?")

    def test_mention_with_delivery_word(self) -> None:
        assert is_spam_candidate("Доставка, оплата хорошая, пишите @courier_work")

    def test_mention_with_income_100k_week(self) -> None:
        assert is_spam_candidate("100к/неделю — реально! @info_channel")

    def test_mention_with_income_150k_day(self) -> None:
        assert is_spam_candidate("150к в день, без опыта, @quick_earn")

    def test_script_mix_obfuscated_spam(self) -> None:
        """Смешение скриптов (р@ботниkи, д0ставkу, Тр€буются) + @mention → кандидат."""
        text = (
            "Тр€буются p@ботниkи н@ д0ставkу! "
            "3П от 15Ок в мecяц. Бeз oпытa. "
            "Пиcaть в тг @rabota_dostavka"
        )
        assert is_spam_candidate(text), (
            "Обфусцированный спам с тремя паттернами смешения скриптов + @mention"
        )

    def test_income_15ok_month_candidate(self) -> None:
        """15Ок в месяц (О как ноль) + @mention → кандидат."""
        assert is_spam_candidate("ЗП от 15Ок в месяц, пиши @job_channel")

    def test_script_mix_no_mention_not_candidate(self) -> None:
        """Обфускация БЕЗ @упоминания и БЕЗ телефона → не кандидат для LLM."""
        assert not is_spam_candidate("Тр€буются на доставку, звоните по тел 123-456")

    # --- Новые тесты: телефон как канал контакта ---

    def test_phone_plus_script_mix_candidate(self) -> None:
        """В0Д0СЧЕТЧИК0В (script mix) + телефон → кандидат."""
        text = "Установка В0Д0СЧЕТЧИК0В! Акция до конца недели. Звоните +7(999)123-45-67"
        assert is_spam_candidate(text), "Реклама с номером телефона + обфускация должна быть кандидатом"

    def test_phone_plus_commercial_word_candidate(self) -> None:
        """Телефон + «акция» → кандидат (коммерческая реклама)."""
        assert is_spam_candidate("Пломбировка счётчиков в подарок! Звоните +7(800)555-00-11")

    def test_phone_without_commercial_signal_not_candidate(self) -> None:
        """Телефон в легитимном контексте (горячая линия) → не кандидат."""
        # Нет коммерческих слов, нет script mixing, нет income
        assert not is_spam_candidate(
            "Горячая линия управляющей компании: 8-800-100-10-10"
        )

    def test_phone_plus_courier_earn_candidate(self) -> None:
        """Телефон + «курьеры 100k в день» → кандидат (реальный спам из логов)."""
        text = "Тpебyются кyрьеры!!! 3п от 100k в день. Свободный график, без опыта. Тел: +7(999)000-00-01"
        assert is_spam_candidate(text)

    def test_ls_contact_plus_income_candidate(self) -> None:
        """«Пиши в лс» + income → кандидат (нет @mention и телефона)."""
        assert is_spam_candidate("з/п от 100к в неделю, без опыта, пиши в лс!")

    def test_ls_without_commercial_not_candidate(self) -> None:
        """«Напишите в лс» без коммерческих слов → не кандидат."""
        assert not is_spam_candidate("Кто нашёл ключи от машины — напишите в лс")


# ---------------------------------------------------------------------------
# _parse_verdict(): разбор JSON-ответа LLM
# ---------------------------------------------------------------------------

class TestParseVerdict:
    def test_spam_earn(self) -> None:
        raw = '{"is_spam": true, "category": "earn", "reason": "аномальная оплата 100к/неделю"}'
        v = _parse_verdict(raw)
        assert v.is_spam
        assert v.category == "earn"
        assert "100к" in v.reason

    def test_not_spam(self) -> None:
        raw = '{"is_spam": false, "category": null, "reason": ""}'
        v = _parse_verdict(raw)
        assert not v.is_spam
        assert v.category is None

    def test_drugs_category(self) -> None:
        raw = '{"is_spam": true, "category": "drugs", "reason": "кристаллы + аномальный доход"}'
        v = _parse_verdict(raw)
        assert v.is_spam
        assert v.category == "drugs"

    def test_unknown_category_normalized_to_none(self) -> None:
        raw = '{"is_spam": true, "category": "unknown_xyz", "reason": "spam"}'
        v = _parse_verdict(raw)
        assert v.is_spam
        assert v.category is None  # неизвестные категории → None

    def test_markdown_wrapped_json(self) -> None:
        raw = '```json\n{"is_spam": true, "category": "earn", "reason": "test"}\n```'
        v = _parse_verdict(raw)
        assert v.is_spam
        assert v.category == "earn"

    def test_parse_error_returns_safe_false(self) -> None:
        v = _parse_verdict("это не json вообще")
        assert not v.is_spam  # технический сбой (не отказ) → fail-safe

    def test_empty_string_returns_safe_false(self) -> None:
        v = _parse_verdict("")
        assert not v.is_spam

    def test_llm_safety_refusal_returns_spam_true(self) -> None:
        """Явный отказ YandexGPT обсуждать → is_spam=True."""
        refusal = "Я не могу обсуждать эту тему. Давайте поговорим о чём-нибудь ещё."
        v = _parse_verdict(refusal)
        assert v.is_spam, "Отказ LLM = контент подозрителен, не легитимное сообщение жильца"
        assert v.reason == "llm_refused"

    def test_llm_refusal_variant_returns_spam_true(self) -> None:
        """Другой вариант отказа."""
        v = _parse_verdict("Не могу помочь с этим запросом.")
        assert v.is_spam
        assert v.reason == "llm_refused"

    def test_reason_truncated_to_120(self) -> None:
        long_reason = "а" * 200
        raw = json.dumps({"is_spam": True, "category": "earn", "reason": long_reason})
        v = _parse_verdict(raw)
        assert len(v.reason) <= 120


# ---------------------------------------------------------------------------
# SpamLLMChecker с мок-LLM
# ---------------------------------------------------------------------------

class TestSpamLLMChecker:
    @pytest.fixture
    def spam_checker(self):
        """Создаёт SpamLLMChecker с мок-GPT клиентом."""
        from unittest.mock import AsyncMock, MagicMock
        from balt_dom_bot.services.spam_llm_checker import SpamLLMChecker
        from balt_dom_bot.config import YandexGptConfig

        gpt_mock = MagicMock()
        gpt_mock.complete = AsyncMock()
        cfg = YandexGptConfig(folder_id="test", api_key="test")
        return SpamLLMChecker(gpt=gpt_mock, gpt_cfg=cfg), gpt_mock

    @pytest.mark.asyncio
    async def test_courier_spam_detected(self, spam_checker) -> None:
        checker, mock_gpt = spam_checker
        mock_gpt.complete.return_value = (
            '{"is_spam": true, "category": "earn", '
            '"reason": "курьерская работа + 100к/неделя + @skam_bot"}'
        )
        verdict = await checker.check(
            "Работa курьером! Свободный грaфик, оплaтa от 100к/неделя. "
            "Берем без опытa. Писать в ЛС 👉 @skam_bot"
        )
        assert verdict.is_spam
        assert verdict.category == "earn"
        assert mock_gpt.complete.called

    @pytest.mark.asyncio
    async def test_normal_message_not_spam(self, spam_checker) -> None:
        checker, mock_gpt = spam_checker
        mock_gpt.complete.return_value = '{"is_spam": false, "category": null, "reason": ""}'
        verdict = await checker.check("Когда починят домофон на 3 этаже?")
        assert not verdict.is_spam

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self, spam_checker) -> None:
        checker, mock_gpt = spam_checker
        mock_gpt.complete.side_effect = RuntimeError("API unavailable")
        verdict = await checker.check("Работa курьером @skam_bot 100к/неделю")
        # Fail-safe: ошибка API → is_spam=False, никого не баним
        assert not verdict.is_spam
        assert "api_error" in verdict.reason

    @pytest.mark.asyncio
    async def test_normalized_text_passed_when_obfuscated(self, spam_checker) -> None:
        """Обфусцированный текст → LLM получает нормализованный вариант (не оригинал)."""
        checker, mock_gpt = spam_checker
        mock_gpt.complete.return_value = '{"is_spam": true, "category": "drugs", "reason": "тест"}'
        obfuscated = "Кpиcтaллы зaкaзывaйтe у нac @dealer_bot"
        await checker.check(obfuscated)
        call_args = mock_gpt.complete.call_args
        user_msg = call_args[0][0][-1].text  # последнее сообщение = user
        # LLM видит нормализованный текст, не оригинал с гомоглифами
        assert "Нормализованный" in user_msg
        assert "Кристаллы" in user_msg    # нормализованное слово есть
        assert "Кpиcтaллы" not in user_msg  # оригинал с гомоглифами НЕ передаётся

    @pytest.mark.asyncio
    async def test_drug_emoji_stripped_before_llm(self, spam_checker) -> None:
        """❄️🍁 (emoji-маркеры наркотиков) убраны до отправки в LLM."""
        checker, mock_gpt = spam_checker
        mock_gpt.complete.return_value = '{"is_spam": true, "category": "earn", "reason": "тест"}'
        text_with_emoji = "Только ❄️ и 🍁. Доставка. Оплата криптой @HR_best_work"
        await checker.check(text_with_emoji)
        user_msg = mock_gpt.complete.call_args[0][0][-1].text
        assert "❄️" not in user_msg
        assert "🍁" not in user_msg

    @pytest.mark.asyncio
    async def test_non_obfuscated_passes_single_form(self, spam_checker) -> None:
        """Нормальный текст → LLM получает только одну форму."""
        checker, mock_gpt = spam_checker
        mock_gpt.complete.return_value = '{"is_spam": false, "category": null, "reason": ""}'
        plain = "Добрый день, лифт не работает"
        await checker.check(plain)
        user_msg = mock_gpt.complete.call_args[0][0][-1].text
        assert "Нормализованный" not in user_msg  # нормализация ничего не изменила


# ---------------------------------------------------------------------------
# Интеграция: stub-клиент корректно обрабатывает spam_checker prompt
# ---------------------------------------------------------------------------

class TestStubHandlesSpamChecker:
    @pytest.mark.asyncio
    async def test_stub_returns_not_spam(self) -> None:
        from balt_dom_bot.services.yandex_gpt import StubYandexGptClient
        from balt_dom_bot.prompts.spam_checker import SPAM_CHECKER_SYSTEM_PROMPT
        from balt_dom_bot.services.yandex_gpt import GptMessage

        stub = StubYandexGptClient()
        raw = await stub.complete(
            [
                GptMessage(role="system", text=SPAM_CHECKER_SYSTEM_PROMPT),
                GptMessage(role="user", text="Работа курьером @bot 100к/неделю"),
            ],
            temperature=0.0,
        )
        data = json.loads(raw)
        assert data["is_spam"] is False
