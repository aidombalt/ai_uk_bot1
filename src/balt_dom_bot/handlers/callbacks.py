"""Callback-кнопки в карточке эскалации: одобрить автоответ / игнорировать.

Поведение:
* `e:a:{esc_id}` — атомарно резолвим PENDING→APPROVED, шлём proposed_reply
  в чат ЖК как reply на исходное сообщение жильца, редактируем карточку,
  логируем reply (source='manager_approved').
* `e:i:{esc_id}` — атомарно резолвим PENDING→IGNORED, редактируем карточку.

Idempotency: повторное нажатие на уже разрешённую эскалацию ничего не делает.
"""

from __future__ import annotations

from typing import Any

from balt_dom_bot.config import AppConfig
from balt_dom_bot.handlers.sender import MaxBotEscalationSender, MaxBotReplySender
from balt_dom_bot.log import get_logger
from balt_dom_bot.services.escalation import render_resolved_card
from balt_dom_bot.storage.escalations import EscalationRepo, EscalationStatus
from balt_dom_bot.storage.manager_reply_repo import DraftStatus, ManagerReplyRepo
from balt_dom_bot.storage.message_log import MessageLog

log = get_logger(__name__)

PREFIX_APPROVE = "e:a:"
PREFIX_IGNORE = "e:i:"
PREFIX_MR_FORMATTED = "mr:f:"
PREFIX_MR_ORIGINAL = "mr:o:"


def register_callback_handlers(
    dp: Any,
    *,
    repo: EscalationRepo,
    reply_sender: MaxBotReplySender,
    escalation_sender: MaxBotEscalationSender,
    message_log: MessageLog | None = None,
    cfg: AppConfig | None = None,
    complexes: Any = None,
    global_settings: Any = None,
    bans: Any = None,            # BansRepo
    moderator: Any = None,       # Moderator (для разбана)
    manager_reply_repo: ManagerReplyRepo | None = None,
) -> None:
    @dp.message_callback()
    async def on_callback(event: Any) -> None:  # type: ignore[no-untyped-def]
        callback = getattr(event, "callback", None)
        payload = getattr(callback, "payload", None) if callback else None
        actor = getattr(callback, "user", None) if callback else None
        actor_id = getattr(actor, "user_id", None) if actor else None
        actor_name = getattr(actor, "name", None) or getattr(actor, "first_name", None) if actor else None

        log.info(
            "callback.received",
            payload=payload, actor_id=actor_id, actor_name=actor_name,
        )

        if not payload:
            log.warning("callback.no_payload")
            return

        if payload.startswith(PREFIX_APPROVE):
            esc_id = _parse_id(payload[len(PREFIX_APPROVE):])
            if esc_id is None:
                return
            await _handle_approve(
                event, esc_id=esc_id, actor_id=actor_id,
                repo=repo, reply_sender=reply_sender, escalation_sender=escalation_sender,
                message_log=message_log,
            )
            return

        if payload.startswith(PREFIX_IGNORE):
            esc_id = _parse_id(payload[len(PREFIX_IGNORE):])
            if esc_id is None:
                return
            await _handle_ignore(
                event, esc_id=esc_id, actor_id=actor_id,
                repo=repo, escalation_sender=escalation_sender,
            )
            return

        # Разбан: unban:<chat_id>:<user_id>
        if payload.startswith("unban:"):
            try:
                await _handle_unban_callback(
                    event=event, payload=payload, actor_id=actor_id,
                    complexes=complexes, moderator=moderator, bans=bans,
                )
            except Exception as exc:
                log.exception("unban_callback.crash", error=str(exc))
            return

        # Admin-callback'и (mode/mod/global).
        if payload.startswith("admin:"):
            try:
                await _handle_admin_callback(
                    event=event, payload=payload, actor_id=actor_id,
                    complexes=complexes, global_settings=global_settings,
                )
            except Exception as exc:
                log.exception("admin_callback.crash", error=str(exc), payload=payload)
            return

        # Manager Reply: отправить форматированный вариант.
        if payload.startswith(PREFIX_MR_FORMATTED):
            draft_id = _parse_id(payload[len(PREFIX_MR_FORMATTED):])
            if draft_id is None:
                return
            await _handle_manager_reply(
                event, draft_id=draft_id, send_formatted=True,
                manager_reply_repo=manager_reply_repo,
                reply_sender=reply_sender,
                escalation_sender=escalation_sender,
                message_log=message_log,
            )
            return

        # Manager Reply: отправить оригинал.
        if payload.startswith(PREFIX_MR_ORIGINAL):
            draft_id = _parse_id(payload[len(PREFIX_MR_ORIGINAL):])
            if draft_id is None:
                return
            await _handle_manager_reply(
                event, draft_id=draft_id, send_formatted=False,
                manager_reply_repo=manager_reply_repo,
                reply_sender=reply_sender,
                escalation_sender=escalation_sender,
                message_log=message_log,
            )
            return

        log.warning("callback.unknown_payload", payload=payload)


async def _handle_unban_callback(
    *,
    event: Any,
    payload: str,
    actor_id: int | None,
    complexes: Any,
    moderator: Any,
    bans: Any,
) -> None:
    """Разбан по нажатию inline-кнопки. Authorization: actor должен быть
    manager_user_id чата (или конкретного ЖК).
    """
    if moderator is None or bans is None or complexes is None:
        log.warning("unban_callback.no_deps")
        return
    if actor_id is None:
        return

    parts = payload.split(":")
    if len(parts) < 3:
        return
    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        log.warning("unban_callback.bad_payload", payload=payload)
        return

    # Проверяем, что actor — управляющий ЖК с этим chat_id.
    complex_row = await complexes.find_by_chat(chat_id)
    if complex_row is None:
        try:
            await event.answer(notification="Чат не зарегистрирован")
        except Exception:
            pass
        return
    if complex_row.manager_user_id != actor_id:
        try:
            await event.answer(notification="Нет прав на разбан в этом чате")
        except Exception:
            pass
        log.warning("unban_callback.unauthorized",
                    actor_id=actor_id, complex_id=complex_row.id)
        return

    api_ok = await moderator.unban(
        chat_id=chat_id, user_id=user_id, by_user_id=actor_id,
    )
    msg = "Разбан выполнен" if api_ok else "Разбан в БД ✓ / в Max — не подтвержден"
    try:
        await event.answer(notification=msg)
    except Exception:
        pass


async def _handle_admin_callback(
    *,
    event: Any,
    payload: str,
    actor_id: int | None,
    complexes: Any,
    global_settings: Any,
) -> None:
    """Обработка админских callback'ов. Authorization: actor должен быть
    manager_user_id хотя бы одного ЖК.

    Payload format:
        admin:mode:<complex_id>:<normal|holiday|off>
        admin:mod:<complex_id>:<0|1>
        admin:global:<0|1>
    """
    if complexes is None or global_settings is None:
        log.warning("admin_callback.no_repos")
        return

    if actor_id is None:
        return

    # Авторизация
    admin_jks = await complexes.list_for_manager(actor_id)
    if not admin_jks:
        try:
            await event.answer(notification="Только для управляющих ЖК")
        except Exception:
            pass
        log.warning("admin_callback.unauthorized", actor_id=actor_id)
        return

    parts = payload.split(":")
    if len(parts) < 3:
        return
    action = parts[1]

    if action == "global":
        # admin:global:<0|1>
        new_val = parts[2] == "1"
        await global_settings.set_bot_enabled(new_val)
        try:
            await event.answer(
                notification=f"Бот: {'🟢 ВКЛ' if new_val else '🔴 ВЫКЛ'}"
            )
        except Exception:
            pass
        log.info("admin_callback.global", actor_id=actor_id, enabled=new_val)
        # Перерисуем /status (хочется видеть что состояние сменилось).
        await _resend_status(
            event=event, actor_id=actor_id,
            complexes=complexes, global_settings=global_settings,
        )
        return

    if action == "mode" and len(parts) >= 4:
        # admin:mode:<complex_id>:<value>
        complex_id, value = parts[2], parts[3]
        if value not in ("normal", "holiday", "off"):
            return
        # Authorization per-complex: только за свой ЖК
        if not any(j.id == complex_id for j in admin_jks):
            try:
                await event.answer(notification="Этот ЖК не ваш")
            except Exception:
                pass
            return
        await complexes.set_reply_mode(complex_id, value)
        try:
            label = {"normal": "🟢 обычный", "holiday": "🌴 праздничный", "off": "⏸ выключен"}[value]
            await event.answer(notification=f"Режим: {label}")
        except Exception:
            pass
        log.info("admin_callback.mode", actor_id=actor_id, complex_id=complex_id, mode=value)
        await _resend_status(
            event=event, actor_id=actor_id,
            complexes=complexes, global_settings=global_settings,
        )
        return

    if action == "mod" and len(parts) >= 4:
        # admin:mod:<complex_id>:<0|1>
        complex_id = parts[2]
        new_val = parts[3] == "1"
        if not any(j.id == complex_id for j in admin_jks):
            return
        await complexes.set_auto_delete(complex_id, new_val)
        try:
            await event.answer(
                notification=f"Модерация: {'🛡 ВКЛ' if new_val else '⚪️ ВЫКЛ'}"
            )
        except Exception:
            pass
        log.info("admin_callback.mod", actor_id=actor_id, complex_id=complex_id, value=new_val)
        await _resend_status(
            event=event, actor_id=actor_id,
            complexes=complexes, global_settings=global_settings,
        )
        return

    log.warning("admin_callback.unknown_action", payload=payload)


async def _resend_status(
    *, event: Any, actor_id: int, complexes: Any, global_settings: Any,
) -> None:
    """Шлёт обновлённый /status тому же админу. Не критично, если упадёт —
    пользователь нажмёт /status вручную."""
    try:
        from balt_dom_bot.handlers import admin_commands as admin_cmd
        admin_jks = await complexes.list_for_manager(actor_id)
        bot_enabled = await global_settings.is_bot_enabled()
        # Берём chat_id из callback события (где была кнопка).
        msg = getattr(event, "message", None)
        recipient = getattr(msg, "recipient", None) if msg else None
        chat_id = getattr(recipient, "chat_id", None) if recipient else None
        if chat_id is None:
            chat_id = actor_id  # fallback: личный чат с пользователем
        body = admin_cmd._format_status(admin_jks, bot_enabled)
        global_btn_row = [{
            "text": (
                f"🌍 Глобально: {'🔴 ВЫКЛЮЧИТЬ' if bot_enabled else '🟢 ВКЛЮЧИТЬ'}"
            ),
            "payload": f"admin:global:{0 if bot_enabled else 1}",
        }]
        kb_rows = [global_btn_row] + admin_cmd._build_status_buttons(admin_jks)
        await admin_cmd._send_with_buttons(event.bot, int(chat_id), body, kb_rows)
    except Exception as exc:
        log.warning("admin_callback.resend_failed", error=str(exc))


async def _handle_manager_reply(
    event: Any,
    *,
    draft_id: int,
    send_formatted: bool,
    manager_reply_repo: ManagerReplyRepo | None,
    reply_sender: MaxBotReplySender,
    escalation_sender: MaxBotEscalationSender,
    message_log: MessageLog | None,
) -> None:
    """Обрабатывает нажатие «Отправить форматированный / Отправить оригинал»."""
    if manager_reply_repo is None:
        await _safe_answer(event, "Сервис ответов управляющего недоступен.")
        return

    draft = await manager_reply_repo.get_draft(draft_id)
    if draft is None:
        await _safe_answer(event, "Черновик не найден.")
        return
    if draft.status != DraftStatus.PENDING:
        await _safe_answer(event, f"Уже обработано: {draft.status.value}.")
        return

    text_to_send = (
        (draft.formatted_text or draft.manager_text) if send_formatted
        else draft.manager_text
    )

    # Отправляем ответ жильцу в чат ЖК
    await reply_sender.send_reply(
        chat_id=draft.resident_chat_id,
        text=text_to_send,
        reply_to_mid=draft.resident_mid,
    )

    # Атомарно меняем статус (идемпотентно при двойном нажатии)
    status = DraftStatus.SENT_FORMATTED if send_formatted else DraftStatus.SENT_ORIGINAL
    sent = await manager_reply_repo.mark_sent(draft_id, status)
    if not sent:
        # Уже отправлено другим нажатием — молча завершаем
        await _safe_answer(event, "Уже отправлено.")
        return

    # Логируем
    if message_log is not None:
        try:
            await message_log.log_reply(
                complex_id=draft.complex_id,
                chat_id=draft.resident_chat_id,
                in_reply_to=draft.resident_mid,
                text=text_to_send,
                source="manager_approved",  # совместимо с существующим типом
            )
        except Exception as exc:
            log.warning("manager_reply_cb.log_failed", error=str(exc))

    # Заменяем карточку выбора финальным сообщением без кнопок
    variant = "форматированный ✅" if send_formatted else "оригинал 📝"
    final_text = (
        f"✅ Ответ отправлен в чат ЖК\n"
        f"─────\n"
        f"Вариант: {variant}\n"
        f"«{text_to_send}»"
    )
    if draft.choice_card_mid:
        await escalation_sender.resolve_escalation_card(
            chat_id=draft.notif_chat_id,
            old_message_id=draft.choice_card_mid,
            text=final_text,
        )

    log.info(
        "manager_reply_cb.sent",
        draft_id=draft_id,
        send_formatted=send_formatted,
        complex_id=draft.complex_id,
        resident_chat_id=draft.resident_chat_id,
    )
    label = "форматированный" if send_formatted else "оригинал"
    await _safe_answer(event, f"✅ Ответ ({label}) отправлен жильцу.")


def _parse_id(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        log.warning("callback.bad_id", raw=s)
        return None


async def _handle_approve(
    event: Any,
    *,
    esc_id: int,
    actor_id: int | None,
    repo: EscalationRepo,
    reply_sender: MaxBotReplySender,
    escalation_sender: MaxBotEscalationSender,
    message_log: MessageLog | None,
) -> None:
    existing = await repo.get(esc_id)
    if existing is None:
        await _safe_answer(event, "Эскалация не найдена.")
        return
    if existing.status != EscalationStatus.PENDING:
        await _safe_answer(event, f"Уже обработано ранее: {existing.status.value}.")
        return
    if not existing.proposed_reply:
        await _safe_answer(event, "Автоответ не сформирован — нужен ручной ответ.")
        return

    updated = await repo.resolve(esc_id, status=EscalationStatus.APPROVED, by_user_id=actor_id)
    if updated is None:
        await _safe_answer(event, "Уже обработано параллельно.")
        return

    await reply_sender.send_reply(
        chat_id=updated.chat_id,
        text=updated.proposed_reply,
        reply_to_mid=updated.user_message_id,
    )

    if message_log is not None:
        try:
            await message_log.log_reply(
                complex_id=updated.complex_id,
                chat_id=updated.chat_id,
                in_reply_to=updated.user_message_id,
                text=updated.proposed_reply,
                source="manager_approved",
            )
        except Exception as exc:
            log.warning("callback.log_reply_failed", error=str(exc))

    resolved_text = render_resolved_card(
        original_text=_card_text(updated),
        status=EscalationStatus.APPROVED,
        by_user_id=actor_id,
    )
    await _resolve_both_cards(updated, resolved_text, escalation_sender)

    log.info("callback.approved", esc_id=esc_id, actor_id=actor_id)
    await _safe_answer(event, "✅ Автоответ отправлен.")


async def _handle_ignore(
    event: Any,
    *,
    esc_id: int,
    actor_id: int | None,
    repo: EscalationRepo,
    escalation_sender: MaxBotEscalationSender,
) -> None:
    existing = await repo.get(esc_id)
    if existing is None:
        await _safe_answer(event, "Эскалация не найдена.")
        return
    if existing.status != EscalationStatus.PENDING:
        await _safe_answer(event, f"Уже обработано ранее: {existing.status.value}.")
        return

    updated = await repo.resolve(esc_id, status=EscalationStatus.IGNORED, by_user_id=actor_id)
    if updated is None:
        await _safe_answer(event, "Уже обработано параллельно.")
        return

    resolved_text = render_resolved_card(
        original_text=_card_text(updated),
        status=EscalationStatus.IGNORED,
        by_user_id=actor_id,
    )
    await _resolve_both_cards(updated, resolved_text, escalation_sender)

    log.info("callback.ignored", esc_id=esc_id, actor_id=actor_id)
    await _safe_answer(event, "🙈 Помечено как обработанное вручную.")


async def _resolve_both_cards(
    updated: Any,
    resolved_text: str,
    escalation_sender: MaxBotEscalationSender,
) -> None:
    """Заменяет ОБЕ карточки (личка + чат «Обращения») на финальный текст
    БЕЗ кнопок. Используем delete+resend, потому что Max API не убирает
    inline-клавиатуру через edit_message.
    """
    # Карточка в личке управляющего
    if updated.manager_message_id:
        await escalation_sender.resolve_escalation_card(
            chat_id=updated.manager_chat_id,
            old_message_id=updated.manager_message_id,
            text=resolved_text,
        )
    # Карточка в чате «Обращения»
    if updated.chat_message_id and updated.chat_card_chat_id:
        await escalation_sender.resolve_escalation_card(
            chat_id=updated.chat_card_chat_id,
            old_message_id=updated.chat_message_id,
            text=resolved_text,
        )


def _card_text(row) -> str:
    """Реконструкция компактной шапки карточки для resolved-варианта."""
    cls = row.classification
    name = cls.name or row.user_name or "—"
    return (
        f"📩 Обращение #{row.id} | ЖК: {row.complex_id}\n"
        f"👤 Жилец: {name}\n"
        f"🏷 {cls.theme.value}  ⚡ {cls.urgency.value}  💬 {cls.character.value}\n"
        f"📝 Суть: {cls.summary}\n"
        f"─────────────────\n"
        f"Оригинал: «{row.user_text}»\n\n"
        f"💬 Предложенный автоответ:\n«{row.proposed_reply or '—'}»"
    )


async def _safe_answer(event: Any, text: str) -> None:
    """Отвечает на callback (тост/всплывашка у нажавшего).

    В maxapi `MessageCallback` имеет метод `answer(notification=...)` для этого.
    Без вызова этого метода Max показывает у пользователя долгий «крутящийся»
    индикатор после нажатия кнопки.
    """
    # 1) Канонический путь через event.answer()
    answer = getattr(event, "answer", None)
    if callable(answer):
        try:
            await answer(notification=text)
            return
        except TypeError:
            # старая сигнатура без kwargs
            try:
                await answer(text)
                return
            except Exception as exc:
                log.debug("callback.answer_positional_failed", error=str(exc))
        except Exception as exc:
            log.debug("callback.answer_kw_failed", error=str(exc))

    # 2) Fallback через bot.send_callback напрямую
    callback = getattr(event, "callback", None)
    callback_id = getattr(callback, "callback_id", None)
    bot = getattr(event, "bot", None)
    if callback_id and bot is not None:
        send_cb = getattr(bot, "send_callback", None)
        if callable(send_cb):
            try:
                await send_cb(callback_id=callback_id, notification=text)
                return
            except Exception as exc:
                log.warning("callback.send_callback_failed", error=str(exc))
