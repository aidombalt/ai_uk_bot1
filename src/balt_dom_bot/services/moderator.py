"""Авто-модерация: удаление мата + бан после N страйков.

Это ОПЦИОНАЛЬНАЯ функция, включается per-ЖК галочкой `auto_delete_aggression`
в GUI. По умолчанию выключена — управляющий принимает решение сам.

Раздельные счётчики:
* aggression_count (за мат, спам с явным вредом, провокации) →
  порог `strikes_for_ban` (default 3, жёсткий)
* trolling_count (за повторный troll-spam) →
  порог `trolling_strikes_for_ban` (default 6, мягкий)

Бан срабатывает по любому из порогов. После бана:
1) Записываем в таблицу `bans` (для аудита и разбана)
2) Отправляем уведомление в чат «Обращения» И в личку управляющего
3) Сбрасываем оба счётчика страйков

Защитные правила:
* Никогда не банит управляющего этого ЖК (если указан manager_user_id).
* Все операции best-effort: если бот не имеет прав, логируем и идём дальше.
* Страйки имеют TTL (7 дней) — старые сбрасываются автоматически.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import ComplexInfo
from balt_dom_bot.storage.bans_repo import BansRepo
from balt_dom_bot.storage.strikes_repo import StrikeCounts, StrikesRepo

log = get_logger(__name__)

# Тип хука — async функция, которую вызываем после успешного бана.
# Принимает контекст бана (имя, причина, счётчики) и шлёт нотификацию.
BanNotifier = Callable[..., Awaitable[None]]


class Moderator:
    def __init__(
        self,
        *,
        bot: Any,
        strikes: StrikesRepo,
        bans: BansRepo | None = None,
        ban_notifier: BanNotifier | None = None,
    ):
        self._bot = bot
        self._strikes = strikes
        self._bans = bans
        # Хук для отправки нотификаций о бане. Опциональный — если не задан,
        # бан просто логируется и записывается в БД.
        self._ban_notifier = ban_notifier

    # ----- public API ---------------------------------------------------

    async def handle_aggression(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        user_name: str | None = None,
        message_id: str,
        complex_info: ComplexInfo,
    ) -> None:
        await self._moderate(
            chat_id=chat_id, user_id=user_id, user_name=user_name,
            message_id=message_id, complex_info=complex_info,
            kind="aggression", strike_kind="agg",
        )

    async def handle_spam(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        user_name: str | None = None,
        message_id: str,
        complex_info: ComplexInfo,
        spam_category: str,
    ) -> None:
        """Спам — обрабатывается так же, как агрессия (delete + agg-strike)."""
        await self._moderate(
            chat_id=chat_id, user_id=user_id, user_name=user_name,
            message_id=message_id, complex_info=complex_info,
            kind=f"spam:{spam_category}", strike_kind="agg",
        )

    async def handle_trolling(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        user_name: str | None = None,
        message_id: str,
        complex_info: ComplexInfo,
        register_strike: bool,
    ) -> None:
        """Anti-trolling: юзер дёргает бота с off-topic.

        register_strike=False → просто удалить сообщение, без страйка.
        register_strike=True → удалить + trolling-страйк (мягче agg-страйка).
        """
        if not complex_info.auto_delete_aggression:
            return
        if self._is_manager(user_id, complex_info):
            return

        if register_strike:
            await self._moderate(
                chat_id=chat_id, user_id=user_id, user_name=user_name,
                message_id=message_id, complex_info=complex_info,
                kind="trolling", strike_kind="troll",
            )
            return

        # Без страйка — только delete.
        deleted = await self._delete_message_safe(chat_id=chat_id, message_id=message_id)
        log.info(
            "moderation.trolling_deleted",
            chat_id=chat_id, user_id=user_id, message_deleted=deleted,
        )

    # ----- разбан ------------------------------------------------------

    async def unban(
        self, *, chat_id: int, user_id: int, by_user_id: int | None = None,
    ) -> bool:
        """Снимает бан и возвращает юзера в чат (если возможно).

        Стратегия (best-effort, в порядке убывания вероятности успеха):
        1) Помечаем запись в bans как unbanned_at — это всегда работает.
        2) Пробуем kick(block=False) — некоторые API так разблокируют.
        3) Пробуем add_chat_members — добавить обратно явно.
        Если оба API-вызова упали — bool=False, но запись в БД обновлена,
        и пользователь сможет сам зайти по ссылке (бан в чате уже снят
        на стороне БД, а на стороне Max мог не сняться).
        """
        # 1. Сбрасываем счётчики страйков чтобы не банило сразу заново.
        try:
            await self._strikes.reset(chat_id=chat_id, user_id=user_id)
        except Exception as exc:
            log.warning("moderation.strikes_reset_failed", error=str(exc))

        # 2. Помечаем в БД (всегда работает).
        if self._bans is not None:
            ban_id = await self._bans.find_active_by_chat_user(
                chat_id=chat_id, user_id=user_id,
            )
            if ban_id is not None:
                try:
                    await self._bans.mark_unbanned(ban_id=ban_id, by_user_id=by_user_id)
                except Exception as exc:
                    log.warning("moderation.unban_db_failed", error=str(exc))

        # 3. API: пробуем разблокировать в Max.
        api_ok = False
        # Попытка #1: kick с block=False (некоторые мессенджеры так снимают блок).
        try:
            await self._bot.kick_chat_member(
                chat_id=chat_id, user_id=user_id, block=False,
            )
            api_ok = True
            log.info("moderation.unban_via_kick_unblock",
                     chat_id=chat_id, user_id=user_id)
        except Exception as exc:
            log.info(
                "moderation.unban_kick_unblock_failed",
                error=str(exc), error_type=type(exc).__name__,
            )

        # Попытка #2: add_chat_members.
        if not api_ok:
            try:
                await self._bot.add_chat_members(
                    chat_id=chat_id, user_ids=[user_id],
                )
                api_ok = True
                log.info("moderation.unban_via_add",
                         chat_id=chat_id, user_id=user_id)
            except Exception as exc:
                log.warning(
                    "moderation.unban_add_failed",
                    chat_id=chat_id, user_id=user_id,
                    error=str(exc), error_type=type(exc).__name__,
                    hint=(
                        "Возможно, Max API не позволяет ботам добавлять "
                        "ранее заблокированных юзеров. Запись в БД обновлена; "
                        "пользователю нужно самостоятельно зайти по приглашению."
                    ),
                )

        return api_ok

    # ----- внутренняя кухня ---------------------------------------------

    async def _moderate(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        user_name: str | None,
        message_id: str,
        complex_info: ComplexInfo,
        kind: str,
        strike_kind: str,  # "agg" | "troll"
    ) -> None:
        if not complex_info.auto_delete_aggression:
            return

        if self._is_manager(user_id, complex_info):
            log.info(
                "moderation.skip_manager",
                user_id=user_id, chat_id=chat_id, kind=kind,
            )
            return

        deleted = await self._delete_message_safe(
            chat_id=chat_id, message_id=message_id,
        )
        if user_id is None:
            return

        try:
            if strike_kind == "troll":
                counts = await self._strikes.register_trolling_strike(
                    chat_id=chat_id, user_id=user_id,
                )
            else:
                counts = await self._strikes.register_aggression_strike(
                    chat_id=chat_id, user_id=user_id,
                )
        except Exception as exc:
            log.warning("moderation.strike_failed", error=str(exc))
            return

        log.info(
            "moderation.strike_registered",
            chat_id=chat_id, user_id=user_id, kind=kind,
            aggression_count=counts.aggression,
            trolling_count=counts.trolling,
            agg_threshold=complex_info.strikes_for_ban,
            troll_threshold=complex_info.trolling_strikes_for_ban,
            message_deleted=deleted,
        )

        # Решение о бане: любой из счётчиков превысил свой порог.
        agg_over = counts.aggression >= complex_info.strikes_for_ban
        troll_over = counts.trolling >= complex_info.trolling_strikes_for_ban
        if agg_over or troll_over:
            await self._kick_and_record(
                chat_id=chat_id, user_id=user_id, user_name=user_name,
                complex_info=complex_info, counts=counts,
                ban_reason=("aggression" if agg_over else "trolling"),
                last_kind=kind,
            )

    async def _kick_and_record(
        self,
        *,
        chat_id: int,
        user_id: int,
        user_name: str | None,
        complex_info: ComplexInfo,
        counts: StrikeCounts,
        ban_reason: str,
        last_kind: str,
    ) -> None:
        kicked = await self._kick_user_safe(
            chat_id=chat_id, user_id=user_id,
            agg_count=counts.aggression, troll_count=counts.trolling,
        )

        # Записываем в bans независимо от успеха kick — для аудита.
        # Если kick физически не прошёл (нет прав), всё равно отметим попытку.
        if self._bans is not None:
            try:
                await self._bans.record_ban(
                    chat_id=chat_id, user_id=user_id, user_name=user_name,
                    complex_id=complex_info.id, reason=ban_reason,
                    aggression_count=counts.aggression,
                    trolling_count=counts.trolling,
                )
            except Exception as exc:
                log.warning("moderation.bans_record_failed", error=str(exc))

        # Сбрасываем страйки.
        try:
            await self._strikes.reset(chat_id=chat_id, user_id=user_id)
        except Exception:
            pass

        # Уведомление управляющему — best-effort.
        if self._ban_notifier is not None and kicked:
            try:
                await self._ban_notifier(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_name=user_name,
                    complex_info=complex_info,
                    reason=ban_reason,
                    last_kind=last_kind,
                    aggression_count=counts.aggression,
                    trolling_count=counts.trolling,
                )
            except Exception as exc:
                log.warning("moderation.ban_notify_failed", error=str(exc))

    @staticmethod
    def _is_manager(user_id: int | None, complex_info: ComplexInfo) -> bool:
        return (
            complex_info.manager_user_id is not None
            and user_id == complex_info.manager_user_id
        )

    async def _delete_message_safe(
        self, *, chat_id: int, message_id: str,
    ) -> bool:
        try:
            await self._bot.delete_message(message_id=message_id)
            log.info("moderation.message_deleted", chat_id=chat_id, mid=message_id)
            return True
        except Exception as exc:
            log.warning(
                "moderation.delete_failed",
                chat_id=chat_id, mid=message_id,
                error=str(exc), error_type=type(exc).__name__,
                hint="Боту нужны права администратора в чате ЖК.",
            )
            return False

    async def _kick_user_safe(
        self,
        *,
        chat_id: int,
        user_id: int,
        agg_count: int,
        troll_count: int,
    ) -> bool:
        try:
            await self._bot.kick_chat_member(
                chat_id=chat_id, user_id=user_id, block=True,
            )
            log.warning(
                "moderation.user_banned",
                chat_id=chat_id, user_id=user_id,
                aggression_count=agg_count, trolling_count=troll_count,
            )
            return True
        except Exception as exc:
            log.warning(
                "moderation.kick_failed",
                chat_id=chat_id, user_id=user_id,
                error=str(exc), error_type=type(exc).__name__,
                hint="Боту нужны права администратора в чате ЖК.",
            )
            return False
