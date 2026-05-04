"""FastAPI-приложение GUI: очередь эскалаций, лента+SSE, статистика, ЖК, промты."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from balt_dom_bot.gui.auth import (
    AuthConfig,
    clear_cookie,
    get_current_user,
    issue_token,
    require_user,
    set_cookie,
)
from balt_dom_bot.gui.events import EventBus
from balt_dom_bot.handlers.sender import MaxBotEscalationSender, MaxBotReplySender
from balt_dom_bot.log import get_logger
from balt_dom_bot.services.escalation import render_resolved_card
from balt_dom_bot.storage.complexes_repo import ComplexesRepo
from balt_dom_bot.storage.escalations import EscalationRepo, EscalationStatus
from balt_dom_bot.storage.message_log import MessageLog
from balt_dom_bot.storage.prompts_repo import PromptProvider, PromptsRepo
from balt_dom_bot.storage.users_repo import UserRow, UsersRepo

log = get_logger(__name__)


@dataclass
class GuiDeps:
    auth: AuthConfig
    escalations: EscalationRepo
    complexes: ComplexesRepo
    prompts_repo: PromptsRepo
    prompt_provider: PromptProvider
    users: UsersRepo
    message_log: MessageLog
    reply_sender: MaxBotReplySender
    escalation_sender: MaxBotEscalationSender
    event_bus: EventBus
    db_conn: Any
    global_settings: Any = None  # GlobalSettingsRepo
    bans: Any = None             # BansRepo
    moderator: Any = None        # Moderator (для разбана)
    chat_mode_repo: Any = None   # ChatModeRepo
    quota: Any = None            # QuotaManager


def build_gui_app(deps: GuiDeps) -> FastAPI:
    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    app = FastAPI(title="Балтийский Дом — AI-бот")

    # Глобальный handler для ошибок валидации форм.
    # FastAPI по умолчанию возвращает голый JSON 422 — для GUI-форм это плохой UX.
    # Подменяем на HTML-страницу с понятным сообщением и кнопкой «назад».
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # Для API-вызовов оставляем JSON. GUI определяем по Accept или path.
        accept = request.headers.get("accept", "")
        is_html_request = (
            "text/html" in accept
            or not request.url.path.startswith("/api")
        )
        if not is_html_request:
            return JSONResponse(
                status_code=422,
                content={"detail": exc.errors()},
            )
        # Собираем человекочитаемый список ошибок.
        problems = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", [])[1:]) or "поле"
            msg = err.get("msg", "ошибка")
            if err.get("type") == "missing":
                problems.append(f"«{field}» — обязательное поле, заполните его")
            elif "int_parsing" in err.get("type", ""):
                problems.append(f"«{field}» — должно быть числом")
            else:
                problems.append(f"«{field}»: {msg}")
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "user": None, "active": None,
                "title": "Не получилось сохранить форму",
                "message": "Заполните обязательные поля и попробуйте снова.",
                "problems": problems,
                "back_url": request.headers.get("referer") or "/",
            },
            status_code=400,
        )

    @app.middleware("http")
    async def _inject_globals(request: Request, call_next):
        """Прокидывает текущее состояние bot_enabled в request.state, чтобы
        base.html мог его показывать в навбаре без перевалки через каждый
        endpoint. Кэш у репозитория in-memory, так что overhead — мс."""
        if deps.global_settings is not None:
            try:
                request.state.bot_enabled = await deps.global_settings.is_bot_enabled()
            except Exception:
                request.state.bot_enabled = True
        else:
            request.state.bot_enabled = True
        return await call_next(request)

    # ----- Health check (без auth) ----------------------------------------

    @app.get("/healthz")
    async def healthz():
        try:
            cur = await deps.db_conn.execute("SELECT 1")
            await cur.fetchone()
            return {"status": "ok"}
        except Exception as exc:
            log.exception("healthz.failed", error=str(exc))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DB unavailable")

    # ----- Зависимости (DI) ------------------------------------------------

    async def _user_or_redirect(request: Request) -> UserRow:
        user = await get_current_user(request, deps.auth, deps.users)
        if user is None:
            raise HTTPException(status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})
        return user

    # ----- Аутентификация -------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

    @app.post("/login")
    async def login_submit(
        request: Request, login: str = Form(...), password: str = Form(...),
    ):
        result = await deps.users.get_by_login(login.strip())
        if not result or not deps.users.verify_password(password, result[1]):
            return templates.TemplateResponse(request=request, name="login.html", context={"error": "Неверный логин или пароль"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        user, _ = result
        token = issue_token(deps.auth, user)
        resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        set_cookie(resp, token)
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        clear_cookie(resp)
        return resp

    # ----- Главный layout: dashboard --------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, user: UserRow = Depends(_user_or_redirect)):
        return RedirectResponse("/escalations", status_code=status.HTTP_303_SEE_OTHER)

    # ----- Очередь эскалаций ----------------------------------------------

    @app.get("/escalations", response_class=HTMLResponse)
    async def escalations_page(
        request: Request, user: UserRow = Depends(_user_or_redirect),
    ):
        pending = await deps.escalations.list_pending(limit=50)
        complexes = await deps.complexes.list_all()
        complex_by_id = {c.id: c for c in complexes}
        return templates.TemplateResponse(request=request, name="escalations.html", context={"user": user,
                "items": pending, "complex_by_id": complex_by_id, "active": "escalations"},
        )

    @app.post("/escalations/{esc_id}/approve")
    async def approve_escalation(
        esc_id: int, user: UserRow = Depends(_user_or_redirect),
        custom_text: str = Form(default=""),
    ):
        existing = await deps.escalations.get(esc_id)
        if existing is None or existing.status != EscalationStatus.PENDING:
            raise HTTPException(status.HTTP_409_CONFLICT, "Уже обработано или не найдено")
        text = custom_text.strip() or existing.proposed_reply
        if not text:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нет текста ответа")

        updated = await deps.escalations.resolve(
            esc_id, status=EscalationStatus.APPROVED, by_user_id=user.id,
        )
        if updated is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Параллельно обработано")

        await deps.reply_sender.send_reply(
            chat_id=updated.chat_id, text=text, reply_to_mid=updated.user_message_id,
        )
        try:
            await deps.message_log.log_reply(
                complex_id=updated.complex_id, chat_id=updated.chat_id,
                in_reply_to=updated.user_message_id, text=text, source="manager_approved",
            )
        except Exception as exc:
            log.warning("gui.log_reply_failed", error=str(exc))
        if updated.manager_message_id:
            await deps.escalation_sender.edit_escalation_card(
                manager_chat_id=updated.manager_chat_id,
                manager_message_id=updated.manager_message_id,
                text=render_resolved_card(
                    original_text=f"Обращение #{updated.id}",
                    status=EscalationStatus.APPROVED,
                    by_user_id=user.id,
                ),
            )
        deps.event_bus.publish("escalation_resolved", {"id": esc_id, "status": "APPROVED"})
        return RedirectResponse("/escalations", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/escalations/{esc_id}/ignore")
    async def ignore_escalation(esc_id: int, user: UserRow = Depends(_user_or_redirect)):
        updated = await deps.escalations.resolve(
            esc_id, status=EscalationStatus.IGNORED, by_user_id=user.id,
        )
        if updated is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Уже обработано или не найдено")
        if updated.manager_message_id:
            await deps.escalation_sender.edit_escalation_card(
                manager_chat_id=updated.manager_chat_id,
                manager_message_id=updated.manager_message_id,
                text=render_resolved_card(
                    original_text=f"Обращение #{updated.id}",
                    status=EscalationStatus.IGNORED, by_user_id=user.id,
                ),
            )
        deps.event_bus.publish("escalation_resolved", {"id": esc_id, "status": "IGNORED"})
        return RedirectResponse("/escalations", status_code=status.HTTP_303_SEE_OTHER)

    # ----- Лента активности + SSE -----------------------------------------

    @app.get("/feed", response_class=HTMLResponse)
    async def feed_page(
        request: Request, user: UserRow = Depends(_user_or_redirect),
        complex_id: str | None = None, limit: int = 100,
    ):
        sql = (
            "SELECT m.id, m.complex_id, m.chat_id, m.user_name, m.user_text, "
            "m.classification, m.decision, m.received_at "
            "FROM messages m WHERE 1=1 "
        )
        params: list = []
        if complex_id:
            sql += "AND m.complex_id = ? "
            params.append(complex_id)
        sql += "ORDER BY m.received_at DESC LIMIT ?"
        params.append(min(limit, 200))
        cur = await deps.db_conn.execute(sql, params)
        rows = []
        for r in await cur.fetchall():
            cls = json.loads(r["classification"]) if r["classification"] else None
            dec = json.loads(r["decision"]) if r["decision"] else None
            rows.append({
                "id": r["id"], "complex_id": r["complex_id"], "chat_id": r["chat_id"],
                "user_name": r["user_name"], "user_text": r["user_text"],
                "received_at": r["received_at"], "cls": cls, "dec": dec,
            })
        complexes = await deps.complexes.list_all()
        return templates.TemplateResponse(request=request, name="feed.html", context={"user": user, "items": rows,
                "complexes": complexes, "filter_complex": complex_id,
                "active": "feed"},
        )

    @app.get("/feed/stream")
    async def feed_stream(request: Request):
        # SSE требует auth, но через cookie — делаем мягко (если нет — закрываем)
        user = await get_current_user(request, deps.auth, deps.users)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)

        sub = deps.event_bus.subscribe()

        async def event_generator():
            try:
                # heartbeat при подключении
                yield "event: ping\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                        yield msg
                    except asyncio.TimeoutError:
                        # Periodic heartbeat — иначе прокси режут idle.
                        yield ": heartbeat\n\n"
            finally:
                deps.event_bus.unsubscribe(sub)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # ----- Статистика -----------------------------------------------------

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(request: Request, user: UserRow = Depends(_user_or_redirect)):
        # По темам (за последние 30 дней)
        cur = await deps.db_conn.execute(
            """
            SELECT json_extract(classification, '$.theme') AS theme, COUNT(*) AS n
            FROM messages
            WHERE classification IS NOT NULL
              AND received_at >= datetime('now', '-30 days')
            GROUP BY theme ORDER BY n DESC
            """
        )
        by_theme = [(r["theme"], r["n"]) for r in await cur.fetchall()]

        # По ЖК
        cur = await deps.db_conn.execute(
            """
            SELECT complex_id, COUNT(*) AS n
            FROM messages
            WHERE received_at >= datetime('now', '-30 days')
            GROUP BY complex_id ORDER BY n DESC
            """
        )
        by_complex = [(r["complex_id"] or "—", r["n"]) for r in await cur.fetchall()]

        # Авто vs эскалации
        cur = await deps.db_conn.execute(
            """
            SELECT
              SUM(CASE WHEN json_extract(decision, '$.escalate') = 1 THEN 1 ELSE 0 END) AS escalated,
              SUM(CASE WHEN json_extract(decision, '$.escalate') = 0 THEN 1 ELSE 0 END) AS auto
            FROM messages
            WHERE decision IS NOT NULL
              AND received_at >= datetime('now', '-30 days')
            """
        )
        row = await cur.fetchone()
        auto = (row["auto"] or 0) if row else 0
        escalated = (row["escalated"] or 0) if row else 0

        # Эскалации по причинам
        cur = await deps.db_conn.execute(
            "SELECT reason, COUNT(*) AS n FROM escalations GROUP BY reason ORDER BY n DESC"
        )
        by_reason = [(r["reason"], r["n"]) for r in await cur.fetchall()]

        return templates.TemplateResponse(request=request, name="stats.html", context={"user": user,
                "by_theme": by_theme, "by_complex": by_complex,
                "auto": auto, "escalated": escalated, "by_reason": by_reason,
                "active": "stats"},
        )

    # ----- Конфиг ЖК ------------------------------------------------------

    @app.get("/complexes", response_class=HTMLResponse)
    async def complexes_page(request: Request, user: UserRow = Depends(_user_or_redirect)):
        items = await deps.complexes.list_all()
        return templates.TemplateResponse(request=request, name="complexes.html", context={"user": user, "items": items, "active": "complexes"},
        )

    @app.post("/complexes/upsert")
    async def complexes_upsert(
        request: Request,
        user: UserRow = Depends(_user_or_redirect),
        complex_id: str = Form(...), name: str = Form(...), address: str = Form(...),
        chat_id: int = Form(...),
        # manager_chat_id — опциональное: можно использовать только escalation_chat_id
        # (общий чат «Обращения»), не указывая личного chat_id управляющего.
        # Принимаем как строку с парсингом, чтобы пустое поле не валило валидацию.
        manager_chat_id: str = Form(default=""),
        manager_user_id: str = Form(default=""),
        escalation_chat_id: str = Form(default=""),
        active: str = Form(default="on"),
        escalate_to_manager: str = Form(default=""),
        escalate_to_chat: str = Form(default=""),
        auto_delete_aggression: str = Form(default=""),
        strikes_for_ban: int = Form(default=3),
        trolling_strikes_for_ban: int = Form(default=6),
        reply_mode: str = Form(default="normal"),
        holiday_message: str = Form(default=""),
        daily_replies_limit: int = Form(default=5),
        daily_window_hours: int = Form(default=6),
        chat_mode_enabled: str = Form(default=""),
    ):
        def _opt_int(s: str) -> int | None:
            s = s.strip()
            if not s:
                return None
            try:
                return int(s)
            except ValueError:
                return None

        # Бизнес-валидация: должен быть указан хотя бы один канал куда слать
        # эскалации, иначе бот «слепой» — сообщения пропадут в никуда.
        manager_chat_id_int = _opt_int(manager_chat_id) or 0
        escalation_chat_id_int = _opt_int(escalation_chat_id)
        if not manager_chat_id_int and not escalation_chat_id_int:
            return templates.TemplateResponse(
                request=request,
                name="error.html",
                context={
                    "user": user, "active": "complexes",
                    "title": "Не указан адресат для эскалаций",
                    "message": (
                        "Нужно указать хотя бы одно из: «chat_id личного чата "
                        "управляющего» (manager_chat_id) или «chat_id чата "
                        "Обращений» (escalation_chat_id). Иначе бот не сможет "
                        "пересылать обращения жильцов и они потеряются."
                    ),
                    "back_url": "/complexes",
                },
                status_code=400,
            )

        await deps.complexes.upsert(
            complex_id=complex_id.strip(), name=name.strip(), address=address.strip(),
            chat_id=chat_id, manager_chat_id=manager_chat_id_int,
            active=(active == "on"),
            escalation_chat_id=escalation_chat_id_int,
            escalate_to_manager=(escalate_to_manager == "on"),
            escalate_to_chat=(escalate_to_chat == "on"),
            manager_user_id=_opt_int(manager_user_id),
            auto_delete_aggression=(auto_delete_aggression == "on"),
            strikes_for_ban=strikes_for_ban,
            trolling_strikes_for_ban=trolling_strikes_for_ban,
            reply_mode=reply_mode.strip() or "normal",
            holiday_message=holiday_message.strip() or None,
            daily_replies_limit=daily_replies_limit,
            daily_window_hours=daily_window_hours,
            chat_mode_enabled=(chat_mode_enabled == "on"),
        )
        return RedirectResponse("/complexes", status_code=status.HTTP_303_SEE_OTHER)

    # ----- Глобальный тумблер бота ---------------------------------------

    @app.post("/global/toggle")
    async def global_toggle(user: UserRow = Depends(_user_or_redirect)):
        """Переключает bot_enabled. Это аварийная кнопка — после Off бот
        вообще ни на что не реагирует, даже модерация выключена."""
        if deps.global_settings is None:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        cur = await deps.global_settings.is_bot_enabled()
        await deps.global_settings.set_bot_enabled(not cur)
        return RedirectResponse(
            "/escalations", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/complexes/{complex_id}/delete")
    async def complexes_delete(complex_id: str, user: UserRow = Depends(_user_or_redirect)):
        await deps.complexes.delete(complex_id)
        return RedirectResponse("/complexes", status_code=status.HTTP_303_SEE_OTHER)

    # ----- Баны: список и разбан ----------------------------------------

    @app.get("/bans", response_class=HTMLResponse)
    async def bans_list(
        request: Request,
        user: UserRow = Depends(_user_or_redirect),
    ):
        active = []
        history = []
        if deps.bans is not None:
            try:
                active = await deps.bans.list_active(limit=200)
                history = await deps.bans.list_all(limit=50)
                # history без active
                active_ids = {b.id for b in active}
                history = [b for b in history if b.id not in active_ids]
            except Exception:
                pass
        # Подтягиваем имена ЖК для удобства.
        complexes = await deps.complexes.list_all() if hasattr(deps.complexes, "list_all") else []
        complex_names = {c.id: c.name for c in complexes}
        return templates.TemplateResponse(
            request=request,
            name="bans.html",
            context={
                "user": user, "active": "bans",
                "active_bans": active, "history": history,
                "complex_names": complex_names,
            },
        )

    @app.post("/bans/{ban_id}/unban")
    async def bans_unban(
        ban_id: int, user: UserRow = Depends(_user_or_redirect),
    ):
        if deps.bans is None or deps.moderator is None:
            return RedirectResponse("/bans", status_code=status.HTTP_303_SEE_OTHER)
        # Получаем запись чтобы знать chat/user.
        try:
            all_records = await deps.bans.list_all(limit=500)
            record = next((b for b in all_records if b.id == ban_id), None)
        except Exception:
            record = None
        if record is None:
            return RedirectResponse("/bans", status_code=status.HTTP_303_SEE_OTHER)
        try:
            await deps.moderator.unban(
                chat_id=record.chat_id,
                user_id=record.user_id,
                by_user_id=None,  # из GUI — не привязываем к user_id Max
            )
        except Exception:
            pass
        return RedirectResponse("/bans", status_code=status.HTTP_303_SEE_OTHER)

    # ----- Chat whitelist ----------------------------------------------

    @app.get("/chat_whitelist", response_class=HTMLResponse)
    async def chat_whitelist_list(
        request: Request,
        user: UserRow = Depends(_user_or_redirect),
    ):
        entries = []
        if deps.chat_mode_repo is not None:
            try:
                entries = await deps.chat_mode_repo.list_whitelist()
            except Exception:
                pass
        complexes = await deps.complexes.list_all()
        # Имена ЖК по chat_id.
        complex_by_chat = {c.chat_id: c for c in complexes}
        return templates.TemplateResponse(
            request=request,
            name="chat_whitelist.html",
            context={
                "user": user, "active": "chat_whitelist",
                "entries": entries,
                "complexes": complexes,
                "complex_by_chat": complex_by_chat,
            },
        )

    @app.post("/chat_whitelist/add")
    async def chat_whitelist_add(
        chat_id: int = Form(...),
        user_id: int = Form(...),
        user_name: str = Form(default=""),
        note: str = Form(default=""),
        user: UserRow = Depends(_user_or_redirect),
    ):
        if deps.chat_mode_repo is None:
            return RedirectResponse("/chat_whitelist", status_code=status.HTTP_303_SEE_OTHER)
        try:
            await deps.chat_mode_repo.add_to_whitelist(
                chat_id=chat_id, user_id=user_id,
                user_name=user_name.strip() or None,
                note=note.strip() or None,
                added_by=None,  # из GUI без привязки к Max user_id
            )
        except Exception as exc:
            log.warning("gui.chat_whitelist_add_failed", error=str(exc))
        return RedirectResponse("/chat_whitelist", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/chat_whitelist/{chat_id}/{user_id}/remove")
    async def chat_whitelist_remove(
        chat_id: int, user_id: int,
        user: UserRow = Depends(_user_or_redirect),
    ):
        if deps.chat_mode_repo is not None:
            try:
                await deps.chat_mode_repo.remove_from_whitelist(
                    chat_id=chat_id, user_id=user_id,
                )
            except Exception as exc:
                log.warning("gui.chat_whitelist_remove_failed", error=str(exc))
        return RedirectResponse("/chat_whitelist", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/chat_whitelist/{chat_id}/{user_id}/clear_history")
    async def chat_whitelist_clear_history(
        chat_id: int, user_id: int,
        user: UserRow = Depends(_user_or_redirect),
    ):
        if deps.chat_mode_repo is not None:
            try:
                await deps.chat_mode_repo.clear_history(
                    chat_id=chat_id, user_id=user_id,
                )
            except Exception as exc:
                log.warning("gui.chat_history_clear_failed", error=str(exc))
        return RedirectResponse("/chat_whitelist", status_code=status.HTTP_303_SEE_OTHER)

    # ----- Редактор промтов -----------------------------------------------

    @app.get("/prompts", response_class=HTMLResponse)
    async def prompts_page(request: Request, user: UserRow = Depends(_user_or_redirect)):
        # Гарантируем, что в БД есть обе записи (seed дефолтами).
        from balt_dom_bot.prompts.classifier import CLASSIFIER_SYSTEM_PROMPT
        from balt_dom_bot.prompts.responder import RESPONDER_SYSTEM_PROMPT

        await deps.prompts_repo.get_or_seed("classifier_system", CLASSIFIER_SYSTEM_PROMPT)
        await deps.prompts_repo.get_or_seed("responder_system", RESPONDER_SYSTEM_PROMPT)
        items = await deps.prompts_repo.list_all()
        return templates.TemplateResponse(request=request, name="prompts.html", context={"user": user, "items": items, "active": "prompts"},
        )

    @app.post("/prompts/{name}")
    async def prompts_update(
        name: str, user: UserRow = Depends(_user_or_redirect),
        content: str = Form(...),
    ):
        await deps.prompts_repo.upsert(name, content, by_user_id=user.id)
        deps.prompt_provider.invalidate(name)
        return RedirectResponse("/prompts", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/prompts/{name}/reset")
    async def prompts_reset(name: str, user: UserRow = Depends(_user_or_redirect)):
        """Сброс промта к дефолтному значению из кода (актуальной версии).

        Полезно после обновления бота, когда дефолты в коде стали лучше,
        но в БД сохранились старые seed-овые версии.
        """
        from balt_dom_bot.prompts.classifier import CLASSIFIER_SYSTEM_PROMPT
        from balt_dom_bot.prompts.responder import RESPONDER_SYSTEM_PROMPT
        defaults = {
            "classifier_system": CLASSIFIER_SYSTEM_PROMPT,
            "responder_system": RESPONDER_SYSTEM_PROMPT,
        }
        if name not in defaults:
            return RedirectResponse("/prompts", status_code=status.HTTP_303_SEE_OTHER)
        await deps.prompts_repo.upsert(name, defaults[name], by_user_id=user.id)
        deps.prompt_provider.invalidate(name)
        return RedirectResponse("/prompts", status_code=status.HTTP_303_SEE_OTHER)

    return app
