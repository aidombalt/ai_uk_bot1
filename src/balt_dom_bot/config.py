"""Загрузка и валидация конфигурации.

Конфиг = YAML-файл + переменные окружения (.env). В YAML допустимы плейсхолдеры
вида `${VAR}` или `${VAR:default}`, которые раскрываются перед валидацией.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- env-подстановка в YAML ----------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2) or ""
            return os.environ.get(var, default)

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# --- секция bot ---------------------------------------------------------------

class WebhookConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    path: str = "/webhook"
    public_url: str | None = None


class BotConfig(BaseModel):
    mode: Literal["polling", "webhook"] = "polling"
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)


# --- секция yandex_gpt --------------------------------------------------------

class YandexGptConfig(BaseModel):
    folder_id: str
    api_key: str
    model: str = "yandexgpt"
    model_version: str = "latest"
    classifier_temperature: float = 0.0
    responder_temperature: float = 0.4
    max_tokens: int = 300
    request_timeout_seconds: float = 10.0
    retries: int = 2

    @property
    def model_uri(self) -> str:
        return f"gpt://{self.folder_id}/{self.model}/{self.model_version}"

    @property
    def is_stub(self) -> bool:
        return self.folder_id.startswith("STUB_") or self.api_key.startswith("STUB_")


# --- секция pipeline ----------------------------------------------------------

class ActiveHours(BaseModel):
    from_: str = Field("08:00", alias="from")
    to: str = "22:00"

    model_config = {"populate_by_name": True}


class PipelineConfig(BaseModel):
    confidence_threshold: float = 0.6
    active_hours: ActiveHours = Field(default_factory=ActiveHours)
    silent_characters: list[str] = Field(default_factory=lambda: ["AGGRESSION", "PROVOCATION"])
    always_escalate_themes: list[str] = Field(default_factory=lambda: ["EMERGENCY", "LEGAL_ORG"])


# --- секция complexes ---------------------------------------------------------

class ComplexConfig(BaseModel):
    id: str
    name: str
    address: str
    chat_id: int
    manager_chat_id: int


# --- root ---------------------------------------------------------------------

class CacheConfig(BaseModel):
    backend: Literal["memory", "sqlite", "null"] = "memory"
    ttl_seconds: float = 3600.0
    max_entries: int = 1000  # только для memory
    gc_interval_seconds: float = 600.0  # только для sqlite


class AppConfig(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    yandex_gpt: YandexGptConfig
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    complexes: list[ComplexConfig] = Field(default_factory=list)
    default_manager_chat_id: int | None = None
    db_path: str = "./data/bot.sqlite"
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @field_validator("complexes")
    @classmethod
    def _unique_chat_ids(cls, v: list[ComplexConfig]) -> list[ComplexConfig]:
        seen: set[int] = set()
        for c in v:
            if c.chat_id in seen:
                raise ValueError(f"Duplicate complex chat_id={c.chat_id}")
            seen.add(c.chat_id)
        return v

    def find_complex_by_chat(self, chat_id: int) -> ComplexConfig | None:
        return next((c for c in self.complexes if c.chat_id == chat_id), None)


# --- env-обёртка --------------------------------------------------------------

class Env(BaseSettings):
    """Минимум, что должно быть в окружении."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MAX_BOT_TOKEN: str = "STUB_TOKEN_REPLACE_ME"
    YANDEX_FOLDER_ID: str = "STUB_FOLDER_ID"
    YANDEX_API_KEY: str = "STUB_API_KEY"
    BALT_DOM_CONFIG: str = "./config.yaml"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["console", "json"] = "console"
    # GUI
    GUI_ENABLED: bool = True
    GUI_HOST: str = "0.0.0.0"
    GUI_PORT: int = 8000
    GUI_SECRET_KEY: str = "CHANGE_ME_TO_RANDOM_32+_CHARS_FOR_PRODUCTION"
    GUI_ADMIN_LOGIN: str = "admin"
    GUI_ADMIN_PASSWORD: str = "admin"  # создаётся при первом старте, если БД пустая

    @property
    def is_stub_token(self) -> bool:
        return self.MAX_BOT_TOKEN.startswith("STUB_")


# --- loader -------------------------------------------------------------------

def load_config(env: Env | None = None) -> tuple[AppConfig, Env]:
    """Возвращает (валидированный AppConfig, Env)."""
    env = env or Env()
    path = Path(env.BALT_DOM_CONFIG)

    use_yaml = False
    if path.exists():
        if path.is_dir():
            # Распространённая ошибка: Docker создал директорию из volume-mount,
            # потому что на хосте не было файла config.yaml. Падать с понятным
            # сообщением, а не загадочным IsADirectoryError.
            raise RuntimeError(
                f"Путь {path} — это директория, а не файл. "
                f"Скорее всего, Docker создал её сам, потому что на хосте нет "
                f"настоящего config.yaml. Решение: на хосте выполните "
                f"`docker compose down && rm -rf config.yaml && "
                f"cp config.example.yaml config.yaml && docker compose up -d`. "
                f"Либо удалите volume `./config.yaml:/app/config.yaml:ro` "
                f"из docker-compose.yml — он опционален."
            )
        if path.is_file():
            use_yaml = True

    if use_yaml:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        raw = _expand_env(raw)
    else:
        # Запуск без YAML: все настройки из env-переменных, ЖК добавляются через GUI.
        raw: dict[str, Any] = {
            "yandex_gpt": {
                "folder_id": env.YANDEX_FOLDER_ID,
                "api_key": env.YANDEX_API_KEY,
            }
        }

    return AppConfig.model_validate(raw), env
