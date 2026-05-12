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
        assert not v.is_spam  # fail-safe: лучше пропустить чем ошибочно забанить

    def test_empty_string_returns_safe_false(self) -> None:
        v = _parse_verdict("")
        assert not v.is_spam

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
        """Если текст обфусцирован — LLM получает оба варианта."""
        checker, mock_gpt = spam_checker
        mock_gpt.complete.return_value = '{"is_spam": true, "category": "drugs", "reason": "тест"}'
        obfuscated = "Кpиcтaллы зaкaзывaйтe у нac @dealer_bot"
        await checker.check(obfuscated)
        # Проверяем что в запросе к LLM присутствует нормализованный вариант
        call_args = mock_gpt.complete.call_args
        user_msg = call_args[0][0][-1].text  # последнее сообщение = user
        assert "Нормализованный" in user_msg or "Кристаллы" in user_msg

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
