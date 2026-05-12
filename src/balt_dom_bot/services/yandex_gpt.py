"""Клиент YandexGPT 5.1 Pro поверх Foundation Models API.

Эндпоинт:  POST https://llm.api.cloud.yandex.net/foundationModels/v1/completion
Auth:      Authorization: Api-Key <key>, x-folder-id: <folder>

Предоставляет два режима:
  * `RealYandexGptClient` — реальные HTTP-запросы.
  * `StubYandexGptClient`  — детерминированные заглушки на этапе разработки.

Выбор делается на этапе DI (см. app.py) по флагу `yandex_gpt.is_stub`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from balt_dom_bot.config import YandexGptConfig
from balt_dom_bot.log import get_logger

log = get_logger(__name__)

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class GptMessage:
    role: Role
    text: str


class YandexGptClient(Protocol):
    async def complete(
        self,
        messages: list[GptMessage],
        *,
        temperature: float,
        max_tokens: int | None = None,
    ) -> str: ...


# --- real ---------------------------------------------------------------------


class RealYandexGptClient:
    """Тонкий клиент к /foundationModels/v1/completion."""

    URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self, cfg: YandexGptConfig):
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=cfg.request_timeout_seconds,
            headers={
                "Authorization": f"Api-Key {cfg.api_key}",
                "x-folder-id": cfg.folder_id,
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[GptMessage],
        *,
        temperature: float,
        max_tokens: int | None = None,
    ) -> str:
        body = {
            "modelUri": self._cfg.model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": max_tokens or self._cfg.max_tokens,
            },
            "messages": [{"role": m.role, "text": m.text} for m in messages],
        }

        last_err: Exception | None = None
        for attempt in range(self._cfg.retries + 1):
            try:
                resp = await self._client.post(self.URL, json=body)
                resp.raise_for_status()
                data = resp.json()
                return data["result"]["alternatives"][0]["message"]["text"]
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                last_err = exc
                log.warning(
                    "yandex_gpt.retry",
                    attempt=attempt,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if attempt < self._cfg.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        assert last_err is not None
        raise last_err


# --- stub ---------------------------------------------------------------------


class StubYandexGptClient:
    """Никаких сетевых вызовов. Отвечает шаблонами, чтобы pipeline проходил end-to-end.

    Маленький routing по контенту system-промта:
      * содержит «классификатор» / «JSON» — отдаём заглушечный классификатор-JSON;
      * иначе — отдаём нейтральный текстовый ответ для генератора.
    """

    async def complete(
        self,
        messages: list[GptMessage],
        *,
        temperature: float,
        max_tokens: int | None = None,
    ) -> str:
        system = next((m.text for m in messages if m.role == "system"), "")
        log.debug("yandex_gpt.stub", role_count=len(messages), temperature=temperature)

        # Детектор спама: маркер [SPAM_CHECK] уникален для spam_checker prompt.
        # Возвращаем is_spam=false — заглушка не банит никого.
        if "[SPAM_CHECK]" in system:
            return '{"is_spam": false, "category": null, "reason": ""}'

        is_classifier = "JSON" in system or "классификатор" in system.lower()
        if is_classifier:
            # Заглушечная JSON-классификация. LlmClassifier увидит её, не сможет
            # точно разобрать тему — и сделает fallback на StubClassifier.
            # Возвращаем валидный, но «низкоуверенный» JSON.
            return (
                '{"theme":"OTHER","urgency":"LOW","character":"QUESTION",'
                '"name":null,"summary":"(stub LLM)","confidence":0.3}'
            )

        # Текстовый stub для генератора.
        return (
            "Здравствуйте, спасибо за обращение. Информация принята и передана "
            "специалистам. О результатах будет сообщено дополнительно."
        )

    async def aclose(self) -> None:
        return None


def build_yandex_gpt_client(cfg: YandexGptConfig) -> YandexGptClient:
    if cfg.is_stub:
        log.warning("yandex_gpt.using_stub", reason="STUB_* placeholder credentials")
        return StubYandexGptClient()
    return RealYandexGptClient(cfg)
