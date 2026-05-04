"""Pipeline: classify → решить (ответ / эскалация / молчание) → выполнить → залогировать."""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING, Any

from balt_dom_bot.config import AppConfig
from balt_dom_bot.log import get_logger
from balt_dom_bot.models import (
    AddressedTo,
    Character,
    Classification,
    ComplexInfo,
    DEFAULT_HOLIDAY_MESSAGE,
    IncomingMessage,
    PipelineDecision,
    ReplyMode,
    Theme,
)
from balt_dom_bot.services.classifier import Classifier, is_off_topic
from balt_dom_bot.services import spam_detector
from balt_dom_bot.services.escalation import Escalator
from balt_dom_bot.services.responder import Responder

if TYPE_CHECKING:
    from balt_dom_bot.handlers.sender import MaxBotEscalationSender
    from balt_dom_bot.services.chat_context import ChatContextManager
    from balt_dom_bot.services.chat_responder import ChatResponder
    from balt_dom_bot.services.cooldown import CooldownManager
    from balt_dom_bot.services.fragment_troll import FragmentTrollDetector
    from balt_dom_bot.services.moderator import Moderator
    from balt_dom_bot.services.quota_manager import QuotaManager
    from balt_dom_bot.services.recent_reply_tracker import RecentReplyTracker
    from balt_dom_bot.storage.chat_mode_repo import ChatModeRepo
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    from balt_dom_bot.storage.global_settings_repo import GlobalSettingsRepo
    from balt_dom_bot.storage.message_log import MessageLog

log = get_logger(__name__)


def _build_complex_info(complex_row: Any, cfg: AppConfig) -> ComplexInfo:
    """Свёртка ComplexRow → ComplexInfo с учётом дефолтов из конфига."""
    from balt_dom_bot.models import ReplyMode
    try:
        mode = ReplyMode(complex_row.reply_mode)
    except (ValueError, AttributeError):
        mode = ReplyMode.NORMAL
    return ComplexInfo(
        id=complex_row.id,
        name=complex_row.name,
        address=complex_row.address,
        manager_chat_id=complex_row.manager_chat_id or cfg.default_manager_chat_id or 0,
        escalation_chat_id=complex_row.escalation_chat_id,
        escalate_to_manager=complex_row.escalate_to_manager,
        escalate_to_chat=complex_row.escalate_to_chat,
        manager_user_id=complex_row.manager_user_id,
        auto_delete_aggression=complex_row.auto_delete_aggression,
        strikes_for_ban=complex_row.strikes_for_ban,
        trolling_strikes_for_ban=getattr(complex_row, "trolling_strikes_for_ban", 6),
        reply_mode=mode,
        holiday_message=complex_row.holiday_message,
        daily_replies_limit=getattr(complex_row, "daily_replies_limit", 5),
        daily_window_hours=getattr(complex_row, "daily_window_hours", 6),
        chat_mode_enabled=bool(getattr(complex_row, "chat_mode_enabled", False)),
    )


class ReplySender:
    """Отдельный интерфейс — нужен только для отправки публичного ответа в чат."""

    async def send_reply(self, *, chat_id: int, text: str, reply_to_mid: str | None) -> None:
        raise NotImplementedError


def _format_auto_reply_notification(
    *,
    complex_info: ComplexInfo,
    msg: IncomingMessage,
    cls: Classification,
    reply_text: str,
) -> str:
    """Форматирует уведомление о автоответе для чата «Обращения» (без кнопок)."""
    name = cls.name or msg.user_name or "—"
    return (
        f"🤖 Автоответ · {complex_info.name}\n"
        f"👤 {name} · 🏷 {cls.theme.value} · 💬 {cls.character.value}\n"
        f"📝 «{msg.text}»\n"
        f"─────\n"
        f"✅ {reply_text}"
    )


class Pipeline:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        classifier: Classifier,
        responder: Responder,
        escalator: Escalator,
        reply_sender: ReplySender,
        complexes: ComplexesRepo,
        message_log: MessageLog | None = None,
        notifier: "MaxBotEscalationSender | None" = None,
        moderator: "Moderator | None" = None,
        global_settings: "GlobalSettingsRepo | None" = None,
        cooldown: "CooldownManager | None" = None,
        quota: "QuotaManager | None" = None,
        chat_mode_repo: "ChatModeRepo | None" = None,
        chat_responder: "ChatResponder | None" = None,
        chat_context: "ChatContextManager | None" = None,
        recent_replies: "RecentReplyTracker | None" = None,
        fragment_troll: "FragmentTrollDetector | None" = None,
    ):
        self._cfg = cfg
        self._classifier = classifier
        self._responder = responder
        self._escalator = escalator
        self._reply_sender = reply_sender
        self._complexes = complexes
        self._log = message_log
        self._notifier = notifier
        self._moderator = moderator
        self._global_settings = global_settings
        self._cooldown = cooldown
        self._quota = quota
        self._chat_mode_repo = chat_mode_repo
        self._chat_responder = chat_responder
        self._chat_context = chat_context
        self._recent_replies = recent_replies
        self._fragment_troll = fragment_troll

    async def handle(self, msg: IncomingMessage) -> PipelineDecision:
        # === ИЕРАРХИЯ ПРОВЕРОК (см. документацию архитектуры) ===
        # Уровень 1: Глобальный тумблер. Если выключен — бот молчит ВЕЗДЕ,
        # даже модерация не работает. Аварийный stop-switch.
        if self._global_settings is not None:
            if not await self._global_settings.is_bot_enabled():
                log.debug("pipeline.skip_global_disabled", chat_id=msg.chat_id)
                return PipelineDecision(
                    classification=Classification(
                        theme=Theme.OTHER, urgency="LOW",  # type: ignore[arg-type]
                        character=Character.QUESTION,
                        summary="(бот глобально выключен)", confidence=0.0,
                    ),
                )

        # Уровень 2: ЖК существует и активен (find_by_chat фильтрует по active=1).
        complex_row = await self._complexes.find_by_chat(msg.chat_id)
        if complex_row is None:
            log.debug("pipeline.skip_unknown_chat", chat_id=msg.chat_id)
            empty = PipelineDecision(
                classification=Classification(
                    theme=Theme.OTHER,
                    urgency="LOW",  # type: ignore[arg-type]
                    character=Character.QUESTION,
                    summary="(чат не зарегистрирован)",
                    confidence=0.0,
                ),
            )
            await self._safe_log_incoming(msg, complex_id=None, cls=None, decision=None)
            return empty

        # Уровень 3: Сообщение от управляющего → игнор.
        if (
            complex_row.manager_user_id is not None
            and msg.user_id == complex_row.manager_user_id
        ):
            log.info(
                "pipeline.skip_manager",
                user_id=msg.user_id, complex_id=complex_row.id,
            )
            return PipelineDecision(
                classification=Classification(
                    theme=Theme.OTHER, urgency="LOW",  # type: ignore[arg-type]
                    character=Character.QUESTION,
                    summary="(сообщение от управляющего)", confidence=0.0,
                ),
            )

        # Spam-детектор: быстрая regex-проверка ДО LLM. Срабатывает на жаргон
        # наркотиков, реклама криптобирж, лёгкий заработок, эзотерика и т.п.
        # Защищён whitelist'ом (gov.spb.ru/pravo.gov.ru/dom.gosuslugi.ru/...) —
        # пересланный закон или ссылка на жилкомитет НЕ считаются спамом.
        spam_v = spam_detector.detect(msg.text)
        if spam_v.is_spam:
            log.warning(
                "pipeline.spam_detected",
                category=spam_v.category,
                confidence=spam_v.confidence,
                matched=spam_v.matched[:5],
                user_id=msg.user_id,
                preview=msg.text[:80],
            )
            complex_info = _build_complex_info(complex_row, self._cfg)
            await self._handle_spam(msg, complex_info, spam_v)
            return PipelineDecision(
                classification=Classification(
                    theme=Theme.OTHER, urgency="LOW",  # type: ignore[arg-type]
                    character=Character.AGGRESSION,  # ближе всего по семантике для GUI
                    summary=f"[спам:{spam_v.category}] {msg.text[:50]}",
                    confidence=spam_v.confidence,
                ),
                escalate=True,
                escalation_reason=f"spam_{spam_v.category}",
                reply_text=None,  # silent — публично не отвечаем
            )

        # FRAGMENT TROLL DETECTOR: ловим дробный мат
        # («ук» + «это» + «ху» + «ёвая» от одного юзера за минуту).
        # Каждое сообщение по отдельности невинно, но склейка содержит мат.
        # Стоит ВЫШЕ классификации потому что LLM этот трюк не ловит —
        # она видит только текущее сообщение.
        # Применяем только если у юзера есть user_id (чтобы группировать).
        # И только если у нас есть moderator + auto_delete_aggression включено.
        if (
            self._fragment_troll is not None
            and msg.user_id is not None
            and self._moderator is not None
        ):
            self._fragment_troll.add(
                chat_id=msg.chat_id, user_id=msg.user_id,
                message_id=msg.message_id, text=msg.text,
            )
            complex_info_for_troll = _build_complex_info(complex_row, self._cfg)
            if complex_info_for_troll.auto_delete_aggression:
                fragment_mids = self._fragment_troll.detect_in_recent(
                    chat_id=msg.chat_id, user_id=msg.user_id,
                )
                if fragment_mids:
                    log.warning(
                        "pipeline.fragment_troll_detected",
                        chat_id=msg.chat_id, user_id=msg.user_id,
                        mids=fragment_mids,
                    )
                    # Удаляем ВСЕ части. Применяем страйк один раз
                    # (handle_aggression сам инкрементирует и банит при пороге).
                    for part_mid in fragment_mids:
                        try:
                            await self._moderator.handle_aggression(
                                chat_id=msg.chat_id,
                                user_id=msg.user_id,
                                user_name=msg.user_name,
                                message_id=part_mid,
                                complex_info=complex_info_for_troll,
                            )
                        except Exception as exc:
                            log.warning(
                                "pipeline.fragment_troll_delete_failed",
                                mid=part_mid, error=str(exc),
                            )
                    # Чистим буфер этого юзера чтобы не сработать повторно
                    # на следующем сообщении того же юзера.
                    self._fragment_troll.clear(
                        chat_id=msg.chat_id, user_id=msg.user_id,
                    )
                    return PipelineDecision(
                        classification=Classification(
                            theme=Theme.OTHER, urgency="LOW",  # type: ignore[arg-type]
                            character=Character.AGGRESSION,
                            summary="[fragment-troll] дробный мат",
                            confidence=1.0,
                            addressed_to=AddressedTo.UC,
                        ),
                        escalate=True,
                        escalation_reason="fragment_troll",
                        reply_text=None,
                    )

        # КОНТЕКСТ ЧАТА: подмешиваем последние сообщения чата (от любых
        # юзеров) в промт классификатора. Это позволяет LLM понять, что
        # «да, козлы» — это продолжение треда «надо менять УК» (residents),
        # а не самостоятельная агрессия в адрес УК.
        chat_context_entries = None
        if self._chat_context is not None:
            chat_context_entries = self._chat_context.get_context(
                chat_id=msg.chat_id, exclude_last=False,
            )

        cls = await self._classifier.classify(
            text=msg.text,
            author_name=msg.user_name,
            chat_context=chat_context_entries,
        )

        # Записываем ТЕКУЩЕЕ сообщение в буфер контекста чата ПОСЛЕ того
        # как контекст уже использован классификатором (иначе текущее
        # сообщение попало бы в свой же контекст). bot_reply допишем
        # позже через attach_bot_reply, когда отправим ответ.
        if self._chat_context is not None:
            self._chat_context.add(
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                user_name=msg.user_name,
                text=msg.text,
            )

        # Проверяем заранее: жилец в whitelist для болтания?
        # Если да — НЕ пропускаем его сообщения через off-topic skip даже если
        # они выглядят как residents-болтовня. Тревожники часто пишут именно
        # эмоционально-риторически, и chat-mode должен их слышать.
        # ВАЖНО: это касается ТОЛЬКО off-topic skip. Spam detector, AGGRESSION
        # модерация — продолжают работать (они выше по pipeline или ниже по
        # отдельным веткам).
        complex_info_early = _build_complex_info(complex_row, self._cfg)
        is_chat_mode_user = (
            complex_info_early.chat_mode_enabled
            and self._chat_mode_repo is not None
            and msg.user_id is not None
            and await self._chat_mode_repo.is_whitelisted(
                chat_id=msg.chat_id, user_id=msg.user_id,
            )
        )

        # Off-topic фильтр: болтовня жильцов между собой, не адресованная УК.
        # Бот молчит И не логирует как обращение, чтобы не засирать ленту/БД.
        # Агрессия/провокация при этом всё равно эскалируется — эта проверка
        # учитывается внутри is_off_topic.
        # Whitelisted chat-mode users обходят фильтр ТОЛЬКО для residents-сообщений
        # (эмоциональные жалобы тревожника). Для UNCLEAR сообщений (бессодержательные
        # «гм», «ну ну», крики) НЕ обходим — бот молчит, чтобы chat-mode не
        # генерировал спам в ответ на каждую реплику.
        chat_mode_bypass_offtopic = (
            is_chat_mode_user
            and cls.addressed_to == AddressedTo.RESIDENTS
        )
        if not chat_mode_bypass_offtopic and is_off_topic(
            msg.text, cls.theme, cls.character, cls.addressed_to,
        ):
            log.info(
                "pipeline.skip_off_topic",
                preview=msg.text[:60],
                theme=cls.theme.value,
                character=cls.character.value,
                bot_mentioned=msg.bot_mentioned,
                addressed_to=cls.addressed_to.value if cls.addressed_to else None,
            )
            # Anti-trolling: считаем «троллинговые» сигналы — намеренное
            # дёргание бота, которое нужно гасить. Триггеры (любой → counter):
            #   1) bot_mentioned — явный тег бота с off-topic вопросом
            #   2) addressed_to=UNCLEAR — короткие бессмысленные сообщения
            #      («Ало?», «Эй», «АЛОООО») — LLM сам отметил как unclear
            #   3) Юзер уже в reply-cooldown — бот недавно ответил, замолчал,
            #      а юзер продолжает писать off-topic. Это явный спам.
            #
            # Прогрессия: 1й silent, 2й delete, 3+ delete+strike,
            # после strikes_for_ban → kick.
            #
            # ВАЖНО: addressed_to=RESIDENTS (нормальная болтовня жильцов между
            # собой) не триггерит ничего — это здоровая активность чата.
            user_in_cooldown = (
                self._cooldown is not None
                and self._cooldown.should_silence_reply(
                    chat_id=msg.chat_id, user_id=msg.user_id,
                )
            )
            is_trolling_signal = (
                msg.bot_mentioned
                or cls.addressed_to == AddressedTo.UNCLEAR
                or user_in_cooldown
            )
            if is_trolling_signal and self._cooldown is not None:
                count = self._cooldown.register_offtopic_mention(
                    chat_id=msg.chat_id, user_id=msg.user_id,
                )
                log.info(
                    "pipeline.trolling_signal",
                    count=count, user_id=msg.user_id, chat_id=msg.chat_id,
                    mention=msg.bot_mentioned,
                    addressed_to=cls.addressed_to.value if cls.addressed_to else None,
                    in_cooldown=user_in_cooldown,
                )
                if count >= 2 and self._moderator is not None:
                    complex_info = _build_complex_info(complex_row, self._cfg)
                    try:
                        await self._moderator.handle_trolling(
                            chat_id=msg.chat_id,
                            user_id=msg.user_id,
                            user_name=msg.user_name,
                            message_id=msg.message_id,
                            complex_info=complex_info,
                            register_strike=count >= 3,
                        )
                    except Exception as exc:
                        log.exception("pipeline.trolling_handle_crash", error=str(exc))
                # Уведомление в чат «Обращения» о подозрительной активности.
                # Шлём НЕ при каждом удалении (это спам), а на milestones:
                # 3, 6, 9, 12... — каждое 3-е событие. Так управляющий видит
                # «вот этот юзер уже стабильно троллит», не получая уведомление
                # на каждый удалённый «Ало».
                # ВАЖНО: try/except — даже если нотификация упадёт, удаление
                # сообщения уже произошло, основная функция не нарушена.
                if (
                    count >= 3 and count % 3 == 0
                    and self._notifier is not None
                ):
                    try:
                        complex_info = _build_complex_info(complex_row, self._cfg)
                        if complex_info.escalation_chat_id:
                            label = (
                                f"⚠️ Подозрительная активность в чате «{complex_info.name}»\n\n"
                                f"👤 {msg.user_name or '—'} (id: {msg.user_id})\n"
                                f"📊 Удалено сообщений за последние 5 мин: {count}\n"
                                f"📝 Последнее: «{msg.text[:100]}»\n\n"
                                f"Возможный троллинг. Если это ошибка — "
                                f"сбросьте режим модерации в GUI."
                            )
                            await self._notifier.send_notification_to_chat(
                                chat_id=complex_info.escalation_chat_id,
                                text=label,
                            )
                    except Exception as exc:
                        log.warning(
                            "pipeline.suspicious_notify_failed",
                            error=str(exc),
                        )
            return PipelineDecision(classification=cls)

        complex_info = _build_complex_info(complex_row, self._cfg)

        # Уровень 5: ANTI-FLOOD. Проверка ДО classify не нужна — нам нужно
        # знать character (агрессия → продолжаем модерацию). Проверяем после.
        decision = self._decide(cls, msg.received_at)
        log.info(
            "pipeline.decision",
            theme=cls.theme.value,
            urgency=cls.urgency.value,
            character=cls.character.value,
            confidence=cls.confidence,
            escalate=decision.escalate,
            reason=decision.escalation_reason,
            silent=decision.reply_text is None,
        )

        # Anti-flood: если юзер в cooldown — глушим публичный ответ И эскалацию
        # (управляющий уже знает, что этот юзер активничает). Модерация всё
        # равно продолжит работать ниже — это критично для безопасности чата.
        in_cooldown = (
            self._cooldown is not None
            and self._cooldown.should_silence_reply(
                chat_id=msg.chat_id, user_id=msg.user_id,
            )
        )
        # Дедуп эскалаций: для обычных эскалаций (low_confidence/llm_error)
        # имеет смысл дедуплить — флудер засрёт инбокс одинаковыми вопросами.
        # Для AGGRESSION/PROVOCATION/SPAM — НЕ дедуплим, управляющий должен
        # видеть каждое такое событие (для решения о бане).
        critical_reasons = {
            "aggression", "provocation",
            "spam_drugs", "spam_crypto", "spam_earn",
            "spam_esoteric", "spam_ads", "spam_mass_mention", "spam_unknown",
        }
        severity = "critical" if decision.escalation_reason in critical_reasons else "normal"
        dedup_escalation = (
            self._cooldown is not None
            and decision.escalate
            and self._cooldown.should_dedupe_escalation(
                chat_id=msg.chat_id, user_id=msg.user_id,
                severity=severity,
            )
        )

        # Уровень 6: DAILY QUOTA. Если жилец уже исчерпал лимит ответов
        # за окно (по умолчанию 5/6h) — глушим публичный ответ И один раз
        # уведомляем управляющего о превышении квоты.
        # Для AGGRESSION/PROVOCATION квота не применяется — модерация
        # должна работать независимо от лимитов.
        is_silent_char = cls.character in {Character(c) for c in self._cfg.pipeline.silent_characters}
        quota_exceeded = False
        if (
            self._quota is not None
            and not is_silent_char
            and msg.user_id is not None
        ):
            quota_exceeded = await self._quota.is_quota_exceeded(
                chat_id=msg.chat_id, user_id=msg.user_id,
                limit=complex_info.daily_replies_limit,
                window_hours=complex_info.daily_window_hours,
            )
            if quota_exceeded:
                log.info(
                    "pipeline.quota_exceeded",
                    user_id=msg.user_id, chat_id=msg.chat_id,
                    limit=complex_info.daily_replies_limit,
                    window_h=complex_info.daily_window_hours,
                )
                # Один раз уведомляем управляющего что жилец заваливает чат.
                # Это сигнал — возможно стоит добавить юзера в chat-mode whitelist.
                already_warned = await self._quota.has_been_warned(
                    chat_id=msg.chat_id, user_id=msg.user_id,
                    window_hours=complex_info.daily_window_hours,
                )
                if not already_warned and self._notifier is not None:
                    try:
                        if complex_info.escalation_chat_id:
                            await self._notifier.send_notification_to_chat(
                                chat_id=complex_info.escalation_chat_id,
                                text=(
                                    f"⚠️ Превышение квоты в чате «{complex_info.name}»\n\n"
                                    f"👤 {msg.user_name or '—'} (id: {msg.user_id})\n"
                                    f"📊 Лимит: {complex_info.daily_replies_limit} "
                                    f"ответов / {complex_info.daily_window_hours} ч.\n"
                                    f"📝 Последнее сообщение: «{msg.text[:120]}»\n\n"
                                    f"Жилец активно пишет в чат. Бот временно молчит. "
                                    f"При желании — добавьте его в режим «болтания» "
                                    f"в GUI → ЖК → Whitelist."
                                ),
                            )
                        await self._quota.mark_warned(
                            chat_id=msg.chat_id, user_id=msg.user_id,
                        )
                    except Exception as exc:
                        log.warning("pipeline.quota_notify_failed", error=str(exc))

        # Уровень 7: CHAT-MODE гейтинг.
        # Если включена фича на ЖК + жилец в whitelist — переключаемся на
        # ChatResponder с историей переписки. Иначе всё как раньше.
        # ВАЖНО: chat-mode не применяется для AGGRESSION/PROVOCATION (модерация
        # должна работать), для silent_characters в целом, и при quota_exceeded
        # (квота — высший приоритет, иначе её можно обойти болтанием).
        # ТАКЖЕ: на addressed_to=UNCLEAR молчим даже в chat-mode. Бессодержательные
        # реплики («ало», «ну?», «гм», «ок хорошо») не должны провоцировать ChatResponder
        # генерировать новые реплики — иначе диалог превращается в спам-машину.
        use_chat_mode = (
            complex_info.chat_mode_enabled
            and self._chat_mode_repo is not None
            and self._chat_responder is not None
            and not is_silent_char
            and not quota_exceeded
            and cls.addressed_to != AddressedTo.UNCLEAR
            and msg.user_id is not None
            and await self._chat_mode_repo.is_whitelisted(
                chat_id=msg.chat_id, user_id=msg.user_id,
            )
        )

        proposed_reply: str | None = None
        if use_chat_mode:
            # Болтание: грузим историю, генерим реплику, добавляем в историю.
            try:
                history = await self._chat_mode_repo.get_history(  # type: ignore[union-attr]
                    chat_id=msg.chat_id, user_id=msg.user_id,
                )
                proposed_reply = await self._chat_responder.respond(  # type: ignore[union-attr]
                    text=msg.text,
                    history=history,
                    complex_info=complex_info,
                    classification=cls,
                )
                log.info(
                    "pipeline.chat_mode_reply",
                    user_id=msg.user_id, chat_id=msg.chat_id,
                    history_len=len(history),
                )
            except Exception as exc:
                log.exception("pipeline.chat_mode_crash", error=str(exc))
                # Fallback на обычный responder, чтобы жилец не остался без ответа.
                use_chat_mode = False

        if not use_chat_mode and not is_silent_char and not quota_exceeded:
            try:
                proposed_reply = await self._responder.respond(
                    classification=cls,
                    original_text=msg.text,
                    complex_info=complex_info,
                )
            except Exception as exc:
                log.exception("pipeline.responder_crash", error=str(exc))
                decision = decision.model_copy(
                    update={"escalate": True, "escalation_reason": "llm_error"}
                )

        if is_silent_char:
            decision = decision.model_copy(update={"reply_text": None})
        elif quota_exceeded:
            # Глушим, но эскалацию high_urgency сохраняем (mark_warned уже сработал
            # для общего превышения; high_urgency — это другая ось).
            decision = decision.model_copy(update={"reply_text": None})
        elif decision.escalation_reason == "high_urgency":
            decision = decision.model_copy(
                update={
                    "reply_text": (
                        "Информация принята, специалисты уведомлены. "
                        "Дополнительные подробности будут опубликованы здесь."
                    )
                }
            )
        else:
            decision = decision.model_copy(update={"reply_text": proposed_reply})

        # Уровень 8: ПРИМЕНЯЕМ reply_mode (normal/holiday/off) — финальная подмена.
        # ВАЖНО: chat-mode не подменяется holiday/off — диалог это диалог.
        if not use_chat_mode:
            decision = self._apply_reply_mode(decision, cls, complex_info)

        # Применяем cooldown на самой границе: это последний фильтр после mode.
        if in_cooldown:
            decision = decision.model_copy(update={"reply_text": None})
            log.info("pipeline.cooldown_silenced_reply",
                     chat_id=msg.chat_id, user_id=msg.user_id)
        if dedup_escalation:
            decision = decision.model_copy(update={"escalate": False})
            log.info("pipeline.cooldown_dedup_escalation",
                     chat_id=msg.chat_id, user_id=msg.user_id)

        # Логируем входящее.
        # АНТИ-ДУБЛИКАТ: если бот уже отвечал на близкую тему за последние
        # минуты в этом чате — гасим reply, чтобы не спамить жильцов
        # одинаковыми шаблонами «обращение принято». Управляющий уже
        # получил эскалацию (если она была), у нас нет необходимости
        # дублировать публичный ответ.
        # ВАЖНО: эскалацию (decision.escalate) НЕ трогаем — там собственная
        # дедупликация в Escalator.
        # Не применяем для chat-mode — там диалог, повторы естественны.
        if (
            decision.reply_text
            and not use_chat_mode
            and self._recent_replies is not None
        ):
            if self._recent_replies.is_recent_duplicate(
                chat_id=msg.chat_id, theme=cls.theme, text=msg.text,
            ):
                log.info(
                    "pipeline.reply_silenced_recent_duplicate",
                    chat_id=msg.chat_id, theme=cls.theme.value,
                )
                decision = decision.model_copy(update={"reply_text": None})

        await self._safe_log_incoming(
            msg, complex_id=complex_info.id, cls=cls, decision=decision
        )

        # Отправляем публичный ответ жильцу.
        if decision.reply_text:
            await self._reply_sender.send_reply(
                chat_id=msg.chat_id,
                text=decision.reply_text,
                reply_to_mid=msg.message_id,
            )
            await self._safe_log_reply(
                complex_id=complex_info.id,
                chat_id=msg.chat_id,
                in_reply_to=msg.message_id,
                text=decision.reply_text,
                source="auto",
            )
            # Регистрируем ответ в cooldown — следующее сообщение от этого
            # юзера может попасть в silence, если бот будет отвечать слишком часто.
            if self._cooldown is not None:
                self._cooldown.register_reply(
                    chat_id=msg.chat_id, user_id=msg.user_id,
                )
            # Регистрируем в дневной квоте — счётчик за окно.
            if self._quota is not None and msg.user_id is not None:
                try:
                    await self._quota.register_reply(
                        chat_id=msg.chat_id, user_id=msg.user_id,
                        window_hours=complex_info.daily_window_hours,
                    )
                except Exception as exc:
                    log.warning("pipeline.quota_register_failed", error=str(exc))
            # Регистрируем в анти-дубликате чтобы следующий похожий вопрос
            # был погашен. Не для chat-mode (там диалог).
            if self._recent_replies is not None and not use_chat_mode:
                try:
                    self._recent_replies.register(
                        chat_id=msg.chat_id, theme=cls.theme, text=msg.text,
                    )
                except Exception as exc:
                    log.warning("pipeline.recent_replies_register_failed", error=str(exc))
            # Дополняем буфер контекста чата ответом бота — чтобы LLM
            # на следующих сообщениях видела что бот уже сказал.
            if self._chat_context is not None:
                try:
                    self._chat_context.attach_bot_reply(
                        chat_id=msg.chat_id, user_id=msg.user_id,
                        reply_text=decision.reply_text,
                    )
                except Exception as exc:
                    log.warning("pipeline.chat_context_attach_failed", error=str(exc))
            # Если это chat-mode — пишем оба сообщения (юзера и бота) в историю.
            if use_chat_mode and self._chat_mode_repo is not None and msg.user_id is not None:
                try:
                    await self._chat_mode_repo.append_message(
                        chat_id=msg.chat_id, user_id=msg.user_id,
                        role="user", text=msg.text,
                    )
                    await self._chat_mode_repo.append_message(
                        chat_id=msg.chat_id, user_id=msg.user_id,
                        role="assistant", text=decision.reply_text,
                    )
                except Exception as exc:
                    log.warning("pipeline.chat_history_append_failed", error=str(exc))

        # Эскалация: карточка с кнопками идёт в личку и/или чат «Обращения».
        if decision.escalate:
            await self._escalator.escalate(
                incoming=msg,
                complex_info=complex_info,
                decision=decision,
                proposed_reply=proposed_reply,
            )
            if self._cooldown is not None:
                self._cooldown.register_escalation(
                    chat_id=msg.chat_id, user_id=msg.user_id,
                )
        else:
            # Автоответ был, эскалация не нужна → дублируем как уведомление в
            # чат «Обращения» (для аудита), если включено. В личку НЕ шлём —
            # там должно быть только то, что требует реакции.
            await self._maybe_notify_chat(complex_info, msg, cls, decision)

        # Авто-модерация: AGGRESSION → удаление + страйк.
        # ВАЖНО: только если агрессия адресована именно УК (addressed_to=uc).
        # Перепалки между жильцами («не тебе, дурень») — НЕ наша забота,
        # бот не модерирует разборки соседей.
        # Если addressed_to=None (LLM не определил) — действуем как раньше,
        # это safety-net.
        is_addressed_to_uc = (
            cls.addressed_to is None or cls.addressed_to == AddressedTo.UC
        )

        # Маркер «подстрекательство к смене УК»: классификатор кладёт это
        # явно в summary при срабатывании. Альтернативно — keyword fallback
        # на случай если LLM не сформулировал так точно (старый промт в БД).
        summary_lc = (cls.summary or "").lower()
        text_lc = (msg.text or "").lower()
        incitement_keywords = (
            "подстрекательств",
            "снести", "сносить",
            "менять ук", "сменить ук", "поменять ук",
            "гнать", "гоните",
            "убрать ук",
            "избав", # «избавиться от ук»
            "другу" + "ю ук",  # «другую УК»
            "к чёрту", "к черту",
            "на свалку",
        )
        is_incitement = any(
            k in summary_lc or k in text_lc
            for k in incitement_keywords
        )
        # Дополнительная проверка: должно быть слово «УК» рядом, чтобы не
        # триггериться на «снести деревья» или «гнать собак» (не про УК).
        if is_incitement:
            has_uk_nearby = any(
                m in text_lc for m in ("ук", "управляющ", "контору")
            )
            is_incitement = is_incitement and has_uk_nearby

        if (
            self._moderator is not None
            and complex_info.auto_delete_aggression
            and is_addressed_to_uc
            and (
                cls.character == Character.AGGRESSION
                or (
                    cls.character == Character.PROVOCATION
                    and is_incitement
                )
            )
        ):
            try:
                # Для PROVOCATION-подстрекательства используем тот же
                # handle_aggression — он удаляет + страйк + бан после порога.
                # Логически это тот же класс деструктива.
                if cls.character == Character.PROVOCATION:
                    log.info(
                        "pipeline.incitement_moderated",
                        chat_id=msg.chat_id, user_id=msg.user_id,
                        preview=msg.text[:80],
                    )
                await self._moderator.handle_aggression(
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                    user_name=msg.user_name,
                    message_id=msg.message_id,
                    complex_info=complex_info,
                )
            except Exception as exc:
                log.exception("pipeline.moderator_crash", error=str(exc))

        return decision

    def _apply_reply_mode(
        self,
        decision: PipelineDecision,
        cls: Classification,
        complex_info: ComplexInfo,
    ) -> PipelineDecision:
        """Применяет per-ЖК reply_mode к уже сформированному decision.

        * NORMAL  — ничего не меняем.
        * HOLIDAY — заменяем reply_text на шаблонный (если он вообще
          был; на агрессию остаёмся silent). Эскалации сохраняются.
        * OFF     — глушим reply_text. Эскалации сохраняются ТОЛЬКО для
          действительно важных случаев: HIGH_URGENCY, AGGRESSION,
          PROVOCATION, EMERGENCY-темы. Для остальных (LOW/MEDIUM
          обращений в OFF-режиме) эскалацию тоже глушим — это и есть
          смысл «бот выключен»: никаких уведомлений на чужие отпуска.
        """
        mode = complex_info.reply_mode
        if mode == ReplyMode.NORMAL:
            return decision

        if mode == ReplyMode.HOLIDAY:
            # В holiday-режиме отвечаем шаблоном на любые «обычные» обращения.
            # На агрессию/провокацию — остаёмся silent (они и так уходят в
            # эскалацию). Подмена работает даже если responder ничего не
            # вернул (low_confidence, ошибка LLM): в режиме отпуска жилец всё
            # равно должен увидеть «мы ответим позже».
            if cls.character in {Character.AGGRESSION, Character.PROVOCATION}:
                return decision
            holiday_text = complex_info.holiday_message or DEFAULT_HOLIDAY_MESSAGE
            return decision.model_copy(update={"reply_text": holiday_text})

        if mode == ReplyMode.OFF:
            # Глушим публичный ответ всегда.
            updates: dict[str, object] = {"reply_text": None}
            # Эскалацию оставляем только для важного.
            keep_escalation = (
                decision.escalation_reason in {"aggression", "provocation",
                                                "high_urgency"}
                or cls.theme == Theme.EMERGENCY
                or (decision.escalation_reason or "").startswith("spam_")
            )
            if not keep_escalation:
                updates["escalate"] = False
            return decision.model_copy(update=updates)

        return decision

    async def _maybe_notify_chat(
        self,
        complex_info: ComplexInfo,
        msg: IncomingMessage,
        cls: Classification,
        decision: PipelineDecision,
    ) -> None:
        if self._notifier is None:
            return
        if not complex_info.escalate_to_chat:
            return
        if not complex_info.escalation_chat_id:
            return
        if not decision.reply_text:
            return
        text = _format_auto_reply_notification(
            complex_info=complex_info, msg=msg, cls=cls, reply_text=decision.reply_text,
        )
        await self._notifier.send_notification_to_chat(
            chat_id=complex_info.escalation_chat_id, text=text,
        )

    async def _handle_spam(
        self,
        msg: IncomingMessage,
        complex_info: ComplexInfo,
        verdict: spam_detector.SpamVerdict,
    ) -> None:
        """Обработка пойманного спама: модерация + уведомление управляющего.

        - Модерация (delete + страйк) — только если включена в настройках ЖК.
        - Уведомление управляющего — ВСЕГДА, даже без модерации, чтобы он
          видел попытки спама и мог решить, нужно ли усилить настройки.
        """
        # 1) Модерация: удалить + страйк (если включена)
        if self._moderator is not None and complex_info.auto_delete_aggression:
            try:
                await self._moderator.handle_spam(
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                    user_name=msg.user_name,
                    message_id=msg.message_id,
                    complex_info=complex_info,
                    spam_category=verdict.category or "unknown",
                )
            except Exception as exc:
                log.exception("pipeline.spam_moderation_crash", error=str(exc))

        # 2) Уведомление: краткая пометка в чат «Обращения», без кнопок —
        # управляющий видит, что было удалено и почему.
        if self._notifier is not None and complex_info.escalate_to_chat and complex_info.escalation_chat_id:
            cat_label = {
                "drugs": "🚨 НАРКОТИКИ",
                "crypto": "💰 крипто-спам",
                "earn": "💼 «лёгкий заработок»",
                "esoteric": "🔮 эзотерика",
                "ads": "📢 реклама",
                "mass_mention": "📣 mass-mention",
            }.get(verdict.category or "", "❓ спам")
            evidence = ", ".join(verdict.matched[:3]) if verdict.matched else "—"
            text = (
                f"🛡 Удалён спам · {complex_info.name}\n"
                f"Категория: {cat_label}\n"
                f"Автор: id={msg.user_id}{f' ({msg.user_name})' if msg.user_name else ''}\n"
                f"Маркеры: {evidence}\n"
                f"─────\n"
                f"Оригинал (не отправлять обратно):\n«{msg.text[:300]}»"
            )
            try:
                await self._notifier.send_notification_to_chat(
                    chat_id=complex_info.escalation_chat_id, text=text,
                )
            except Exception as exc:
                log.warning("pipeline.spam_notify_failed", error=str(exc))

    # --- утилиты ----------------------------------------------------------

    async def _safe_log_incoming(
        self,
        msg: IncomingMessage,
        *,
        complex_id: str | None,
        cls: Classification | None,
        decision: PipelineDecision | None,
    ) -> None:
        if self._log is None:
            return
        try:
            await self._log.log_incoming(
                incoming=msg, complex_id=complex_id,
                classification=cls, decision=decision,
            )
        except Exception as exc:
            log.warning("pipeline.log_incoming_failed", error=str(exc))

    async def _safe_log_reply(
        self,
        *,
        complex_id: str | None,
        chat_id: int,
        in_reply_to: str | None,
        text: str,
        source: str,
    ) -> None:
        if self._log is None:
            return
        try:
            await self._log.log_reply(
                complex_id=complex_id, chat_id=chat_id,
                in_reply_to=in_reply_to, text=text, source=source,  # type: ignore[arg-type]
            )
        except Exception as exc:
            log.warning("pipeline.log_reply_failed", error=str(exc))

    # --- логика принятия решений -----------------------------------------

    def _decide(self, cls: Classification, now: datetime) -> PipelineDecision:
        pcfg = self._cfg.pipeline
        silent = {Character(c) for c in pcfg.silent_characters}
        always = {Theme(t) for t in pcfg.always_escalate_themes}

        if cls.character in silent:
            # КРИТИЧЕСКОЕ ПРАВИЛО: AGGRESSION/PROVOCATION эскалируем и
            # модерируем ТОЛЬКО когда они адресованы УК. Если жильцы ругаются
            # между собой («не тебе, дурень», «отстань» в адрес соседа) — это
            # их разборки, не наша забота. Бот не полицейский в чате жильцов.
            #
            # Если addressed_to=None (LLM не вернул поле — например, fallback
            # после ошибки API) — поведение как раньше: эскалируем. Это
            # safety-net на случай отказа классификатора.
            if cls.addressed_to is not None and cls.addressed_to != AddressedTo.UC:
                # silent-character + не к УК → бот молча игнорирует, как
                # обычный off-topic. Не эскалирует, не удаляет, не банит.
                return PipelineDecision(
                    classification=cls, escalate=False, reply_text=None,
                )
            return PipelineDecision(
                classification=cls,
                escalate=True,
                escalation_reason=(
                    "aggression" if cls.character == Character.AGGRESSION else "provocation"
                ),
            )
        if cls.urgency.value == "HIGH":
            return PipelineDecision(
                classification=cls, escalate=True, escalation_reason="high_urgency"
            )
        if cls.theme in always:
            return PipelineDecision(
                classification=cls, escalate=True, escalation_reason="always_escalate_theme"
            )
        if cls.confidence < pcfg.confidence_threshold:
            return PipelineDecision(
                classification=cls, escalate=True, escalation_reason="low_confidence"
            )
        if not _within_hours(now.time(), pcfg.active_hours.from_, pcfg.active_hours.to):
            return PipelineDecision(
                classification=cls, escalate=True, escalation_reason="after_hours"
            )
        return PipelineDecision(classification=cls, escalate=False)


def _within_hours(t: time, frm: str, to: str) -> bool:
    fh, fm = (int(x) for x in frm.split(":"))
    th, tm = (int(x) for x in to.split(":"))
    f, e = time(fh, fm), time(th, tm)
    return f <= t <= e if f <= e else (t >= f or t <= e)
