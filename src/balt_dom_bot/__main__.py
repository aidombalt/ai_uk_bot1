"""Entry point: bot long-polling + GUI uvicorn в одном asyncio-loop."""

from __future__ import annotations

import asyncio

from balt_dom_bot.app import build_app
from balt_dom_bot.config import load_config
from balt_dom_bot.log import get_logger, setup_logging


async def _run_bot(app) -> None:  # type: ignore[no-untyped-def]
    if app.cfg.bot.mode != "polling":
        raise NotImplementedError("Webhook режим — Sprint 7")
    try:
        await app.bot.delete_webhook()
    except Exception:
        pass
    await app.dp.start_polling(app.bot)


async def _run_gui(app, host: str, port: int) -> None:  # type: ignore[no-untyped-def]
    import uvicorn
    config = uvicorn.Config(
        app.gui_app, host=host, port=port, log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def _run() -> None:
    cfg, env = load_config()
    setup_logging(level=env.LOG_LEVEL, fmt=env.LOG_FORMAT)
    log = get_logger("balt_dom_bot")

    # Production sanity checks
    warnings = _check_production_safety(env)
    for w in warnings:
        log.warning("main.production_warning", detail=w)

    if env.is_stub_token:
        log.warning(
            "main.dry_run",
            reason="MAX_BOT_TOKEN is a STUB placeholder",
            hint="Set MAX_BOT_TOKEN to a real token to start polling",
        )
        # Всё равно поднимаем GUI — можно тестировать без реального бота.
        app = await build_app(cfg, env)
        if app.gui_app is not None:
            log.info("main.dry_run_gui_only", port=env.GUI_PORT)
            try:
                await _run_gui(app, env.GUI_HOST, env.GUI_PORT)
            finally:
                await app.aclose()
            return
        await app.aclose()
        return

    app = await build_app(cfg, env)
    log.info("main.starting", mode=cfg.bot.mode, gui=env.GUI_ENABLED)
    tasks: list[asyncio.Task] = [asyncio.create_task(_run_bot(app))]
    if app.gui_app is not None:
        tasks.append(asyncio.create_task(_run_gui(app, env.GUI_HOST, env.GUI_PORT)))
    try:
        # Ждём первой завершившейся (любая = критическая ошибка).
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc:
                log.exception("main.task_failed", error=str(exc))
                raise exc
    finally:
        await app.aclose()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


def _check_production_safety(env) -> list[str]:  # type: ignore[no-untyped-def]
    """Возвращает список предупреждений для production-конфигурации."""
    warnings: list[str] = []
    if env.GUI_ENABLED:
        if "CHANGE_ME" in env.GUI_SECRET_KEY or len(env.GUI_SECRET_KEY) < 32:
            warnings.append(
                "GUI_SECRET_KEY уязвимый: установите случайную строку ≥32 символов "
                "(например `openssl rand -hex 32`)"
            )
        if env.GUI_ADMIN_PASSWORD == "admin":
            warnings.append(
                "GUI_ADMIN_PASSWORD=admin — установите надёжный пароль перед production"
            )
    if env.LOG_LEVEL.upper() == "DEBUG":
        warnings.append("LOG_LEVEL=DEBUG — для production используйте INFO или WARNING")
    return warnings


if __name__ == "__main__":
    main()
