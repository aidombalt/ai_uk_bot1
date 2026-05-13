"""Сборка приложения: конфиг → БД → seed → сервисы → бот → GUI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from balt_dom_bot.config import AppConfig, Env
from balt_dom_bot.handlers.callbacks import register_callback_handlers
from balt_dom_bot.handlers.lifecycle import (
    register_bot_commands,
    register_lifecycle_handlers,
)
from balt_dom_bot.handlers.messages import register_message_handlers
from balt_dom_bot.handlers.sender import MaxBotEscalationSender, MaxBotReplySender
from balt_dom_bot.log import get_logger
from balt_dom_bot.services.cache import (
    InMemoryResponseCache,
    NullResponseCache,
    ResponseCache,
    SqliteResponseCache,
)
from balt_dom_bot.services.classifier import (
    Classifier,
    LlmClassifier,
    SafetyNetClassifier,
    StubClassifier,
)
from balt_dom_bot.services.escalation import Escalator
from balt_dom_bot.services.pipeline import Pipeline
from balt_dom_bot.services.responder import FaqFirstResponder
from balt_dom_bot.services.spam_llm_checker import SpamLLMChecker
from balt_dom_bot.services.yandex_gpt import build_yandex_gpt_client
from balt_dom_bot.storage.complexes_repo import ComplexesRepo
from balt_dom_bot.storage.db import Database
from balt_dom_bot.storage.escalations import EscalationRepo
from balt_dom_bot.storage.manager_reply_repo import ManagerReplyRepo
from balt_dom_bot.storage.message_log import MessageLog
from balt_dom_bot.storage.prompts_repo import PromptProvider, PromptsRepo
from balt_dom_bot.storage.users_repo import UsersRepo

log = get_logger(__name__)


@dataclass
class App:
    bot: Any
    dp: Any
    pipeline: Pipeline
    cfg: AppConfig
    env: Env
    db: Database
    gui_app: Any  # FastAPI или None
    _gpt_client: Any
    _gc_task: asyncio.Task | None = None

    async def aclose(self) -> None:
        if self._gc_task:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except (asyncio.CancelledError, Exception):
                pass
        close = getattr(self._gpt_client, "aclose", None)
        if close:
            await close()
        # maxapi: закрываем aiohttp-сессию, чтобы не было "Unclosed client session"
        for attr in ("session", "_session"):
            sess = getattr(self.bot, attr, None)
            if sess is not None:
                close_sess = getattr(sess, "close", None)
                if callable(close_sess):
                    try:
                        result = close_sess()
                        if hasattr(result, "__await__"):
                            await result
                    except Exception:
                        pass
                    break
        await self.db.close()


def _build_cache(cfg: AppConfig, db: Database) -> ResponseCache:
    backend = cfg.cache.backend
    if backend == "memory":
        return InMemoryResponseCache(
            ttl_seconds=cfg.cache.ttl_seconds, max_entries=cfg.cache.max_entries
        )
    if backend == "sqlite":
        return SqliteResponseCache(db, ttl_seconds=cfg.cache.ttl_seconds)
    return NullResponseCache()


async def _gc_loop(cache: SqliteResponseCache, interval: float) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            await cache.gc()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("cache.gc_failed", error=str(exc))


async def build_app(cfg: AppConfig, env: Env) -> App:
    from maxapi import Bot, Dispatcher  # type: ignore[import-not-found]

    bot = Bot(token=env.MAX_BOT_TOKEN)
    dp = Dispatcher()

    # БД и репозитории
    db = Database(cfg.db_path)
    await db.connect()
    esc_repo = EscalationRepo(db)
    msg_log = MessageLog(db)
    complexes_repo = ComplexesRepo(db)
    prompts_repo = PromptsRepo(db)
    prompt_provider = PromptProvider(prompts_repo, ttl_seconds=30.0)
    users_repo = UsersRepo(db)
    manager_reply_repo = ManagerReplyRepo(db)

    # Seed: ЖК из YAML и админ из env
    seeded = await complexes_repo.seed_from_yaml(cfg.complexes)
    if seeded:
        log.info("app.seeded_complexes", count=seeded)
    await users_repo.ensure_admin(login=env.GUI_ADMIN_LOGIN, password=env.GUI_ADMIN_PASSWORD)

    # GPT и сервисы
    gpt = build_yandex_gpt_client(cfg.yandex_gpt)

    primary_classifier: Classifier
    if cfg.yandex_gpt.is_stub:
        log.warning("app.classifier_using_stub", reason="STUB credentials")
        primary_classifier = StubClassifier()
    else:
        primary_classifier = LlmClassifier(
            gpt=gpt, gpt_cfg=cfg.yandex_gpt, prompt_provider=prompt_provider,
        )
    classifier: Classifier = SafetyNetClassifier(primary_classifier)

    # LLM-детектор спама: второй слой для хитрого спама, который прошёл мимо regex.
    # При stub-режиме — всегда возвращает is_spam=False (см. StubYandexGptClient).
    spam_llm = SpamLLMChecker(gpt=gpt, gpt_cfg=cfg.yandex_gpt)

    cache = _build_cache(cfg, db)
    responder = FaqFirstResponder(
        gpt=gpt, gpt_cfg=cfg.yandex_gpt, cache=cache, prompt_provider=prompt_provider,
    )

    reply_sender = MaxBotReplySender(bot)
    escalation_sender = MaxBotEscalationSender(bot)
    escalator = Escalator(
        sender=escalation_sender,
        repo=esc_repo,
        manager_reply_repo=manager_reply_repo,
    )

    # Moderator — опциональная авто-модерация мата. Включается per-ЖК.
    from balt_dom_bot.services.cooldown import CooldownManager
    from balt_dom_bot.services.moderator import Moderator
    from balt_dom_bot.storage.bans_repo import BansRepo
    from balt_dom_bot.storage.global_settings_repo import GlobalSettingsRepo
    from balt_dom_bot.storage.strikes_repo import StrikesRepo
    strikes_repo = StrikesRepo(db)
    bans_repo = BansRepo(db)
    global_settings = GlobalSettingsRepo(db)

    # Хук-нотификатор о бане. Шлёт в чат «Обращения» И в личку управляющему.
    # Best-effort: каждая ветка обёрнута в try/except внутри sender'а.
    async def _ban_notifier(
        *, chat_id: int, user_id: int, user_name: str | None,
        complex_info, reason: str, last_kind: str,
        aggression_count: int, trolling_count: int,
    ) -> None:
        unban_payload = f"unban:{chat_id}:{user_id}"
        # Текст для разных причин: agg-страйки ↔ trolling-страйки.
        reason_label = {
            "aggression": "🚫 многократная агрессия / спам",
            "trolling":   "🤖 повторяющийся троллинг бота",
        }.get(reason, reason)
        text = (
            f"🚫 Пользователь забанен в чате «{complex_info.name}»\n\n"
            f"👤 {user_name or '—'} (id: {user_id})\n"
            f"⚖️ Причина: {reason_label}\n"
            f"📋 Последнее: {last_kind}\n"
            f"📊 Страйков: агрессия={aggression_count}, троллинг={trolling_count}\n\n"
            f"Если это ошибка — нажмите кнопку ниже или зайдите в GUI → «🚫 Баны»."
        )
        # 1) В чат «Обращения» — общий лог для всех сотрудников УК.
        if complex_info.escalation_chat_id:
            await escalation_sender.send_with_button(
                chat_id=complex_info.escalation_chat_id,
                text=text,
                button_text="↩️ Разбанить",
                button_payload=unban_payload,
            )
        # 2) Личка управляющего — мгновенное уведомление.
        if complex_info.manager_chat_id:
            await escalation_sender.send_with_button(
                chat_id=complex_info.manager_chat_id,
                text=text,
                button_text="↩️ Разбанить",
                button_payload=unban_payload,
            )

    moderator = Moderator(
        bot=bot, strikes=strikes_repo, bans=bans_repo,
        ban_notifier=_ban_notifier,
    )
    cooldown = CooldownManager(
        replies_per_minute=2,
        cooldown_minutes=10,
        escalation_dedup_minutes=10,
    )

    # Daily quota + chat-mode — новые сервисы для интеллектуальной защиты.
    from balt_dom_bot.services.chat_context import ChatContextManager
    from balt_dom_bot.services.chat_responder import ChatResponder
    from balt_dom_bot.services.completeness_checker import CompletenessChecker
    from balt_dom_bot.services.fragment_troll import FragmentTrollDetector
    from balt_dom_bot.services.quota_manager import QuotaManager
    from balt_dom_bot.services.recent_reply_tracker import RecentReplyTracker
    from balt_dom_bot.services.reply_formatter import ReplyFormatter
    from balt_dom_bot.storage.chat_mode_repo import ChatModeRepo
    quota = QuotaManager(db)
    chat_mode_repo = ChatModeRepo(db)
    chat_responder = ChatResponder(gpt=gpt, gpt_cfg=cfg.yandex_gpt)
    chat_context = ChatContextManager()
    recent_replies = RecentReplyTracker()
    fragment_troll = FragmentTrollDetector()
    completeness = CompletenessChecker(prompt_provider=prompt_provider)
    reply_formatter = ReplyFormatter(gpt=gpt, gpt_cfg=cfg.yandex_gpt)

    pipeline = Pipeline(
        cfg=cfg, classifier=classifier, responder=responder,
        escalator=escalator, reply_sender=reply_sender,  # type: ignore[arg-type]
        complexes=complexes_repo, message_log=msg_log,
        notifier=escalation_sender,
        moderator=moderator,
        global_settings=global_settings,
        cooldown=cooldown,
        quota=quota,
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        chat_context=chat_context,
        recent_replies=recent_replies,
        fragment_troll=fragment_troll,
        completeness=completeness,
        manager_reply_repo=manager_reply_repo,
        spam_llm=spam_llm,
    )

    from balt_dom_bot.handlers.manager_reply import ManagerReplyHandler
    manager_reply_handler = ManagerReplyHandler(
        complexes=complexes_repo,
        manager_reply_repo=manager_reply_repo,
        reply_formatter=reply_formatter,
        escalation_sender=escalation_sender,
        bot=bot,
    )

    register_lifecycle_handlers(dp, cfg)
    register_message_handlers(
        dp, pipeline,
        manager_reply_handler=manager_reply_handler,
        escalations_repo=esc_repo,
    )
    register_callback_handlers(
        dp, repo=esc_repo, reply_sender=reply_sender,
        escalation_sender=escalation_sender, message_log=msg_log, cfg=cfg,
        complexes=complexes_repo, global_settings=global_settings,
        bans=bans_repo, moderator=moderator,
        manager_reply_repo=manager_reply_repo,
        cooldown=cooldown,
    )

    # Seed дефолтных промтов в БД при старте, чтобы они появились в GUI сразу
    # (а не только после первого использования pipeline).
    from balt_dom_bot.prompts.classifier import CLASSIFIER_SYSTEM_PROMPT
    from balt_dom_bot.prompts.completeness import DEFAULT_CLARIFICATION_QUESTION
    from balt_dom_bot.prompts.responder import RESPONDER_SYSTEM_PROMPT
    try:
        await prompts_repo.get_or_seed("classifier_system", CLASSIFIER_SYSTEM_PROMPT)
        await prompts_repo.get_or_seed("responder_system", RESPONDER_SYSTEM_PROMPT)
        await prompts_repo.get_or_seed("completeness_clarification", DEFAULT_CLARIFICATION_QUESTION)
    except Exception as exc:
        log.warning("app.prompts_seed_failed", error=str(exc))

    # Регистрируем список команд для Max (показывает кнопку команд в UI бота).
    try:
        await register_bot_commands(bot)
    except Exception as exc:
        log.warning("app.commands_register_failed", error=str(exc))

    gc_task: asyncio.Task | None = None
    if isinstance(cache, SqliteResponseCache) and cfg.cache.gc_interval_seconds > 0:
        gc_task = asyncio.create_task(_gc_loop(cache, cfg.cache.gc_interval_seconds))

    # GUI (опционально)
    gui_app = None
    if env.GUI_ENABLED:
        from balt_dom_bot.gui.app import GuiDeps, build_gui_app
        from balt_dom_bot.gui.auth import AuthConfig
        from balt_dom_bot.gui.events import EventBus

        event_bus = EventBus()
        gui_deps = GuiDeps(
            auth=AuthConfig(secret_key=env.GUI_SECRET_KEY),
            escalations=esc_repo, complexes=complexes_repo,
            prompts_repo=prompts_repo, prompt_provider=prompt_provider,
            users=users_repo, message_log=msg_log,
            reply_sender=reply_sender, escalation_sender=escalation_sender,
            event_bus=event_bus, db_conn=db.conn,
            global_settings=global_settings,
            bans=bans_repo, moderator=moderator,
            chat_mode_repo=chat_mode_repo, quota=quota,
        )
        gui_app = build_gui_app(gui_deps)
        log.info("app.gui_enabled", port=env.GUI_PORT)

    log.info(
        "app.built",
        gpt_stub=cfg.yandex_gpt.is_stub,
        bot_mode=cfg.bot.mode,
        db_path=cfg.db_path,
        cache_backend=cfg.cache.backend,
        gui=env.GUI_ENABLED,
    )

    return App(
        bot=bot, dp=dp, pipeline=pipeline, cfg=cfg, env=env,
        db=db, gui_app=gui_app, _gpt_client=gpt, _gc_task=gc_task,
    )
