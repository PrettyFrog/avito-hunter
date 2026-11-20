from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

from loguru import logger
import typer

from hunter.core.config import get_settings, load_watchlists
from hunter.core.db import Database, get_db

product_app = typer.Typer(no_args_is_help=False, help="[Deprecated in v2]")
coverage_app = typer.Typer(no_args_is_help=False, help="[Deprecated in v2]")
budget_app = typer.Typer(no_args_is_help=False, help="[Deprecated in v2]")

app = typer.Typer(no_args_is_help=True, help="Personal multi-watchlist deal hunter")
app.add_typer(product_app, name="product")
app.add_typer(coverage_app, name="coverage")
app.add_typer(budget_app, name="budget")
shadow_app = typer.Typer(no_args_is_help=True, help="Shadow probe and evaluation commands")
app.add_typer(shadow_app, name="shadow")


@app.command()
def run(dash: bool = typer.Option(False, help="also start local dashboard")) -> None:
    """Long-running collector + Round 1 + notifications."""
    from hunter.cli import _run

    exit_code = 0
    try:
        asyncio.run(_run(dash=dash))
    except Exception as exc:
        logger.exception("Hunter terminated with error: {}", exc)
        exit_code = 1
        raise
    finally:
        if "PYTEST_CURRENT_TEST" not in os.environ:
            sys.exit(exit_code)


def _create_bootstrap_instance(
    settings: Any, bootstrap_factory: Any, browser_factory: Any
) -> Any:
    from hunter.bootstrap import Bootstrap
    from hunter.transport.browser_factory import create_slot_browser_factory
    from hunter.transport.session_store import SessionStore

    session_store = SessionStore(settings.data_dir / "sessions")
    bf = browser_factory or create_slot_browser_factory(settings, session_store=session_store)
    if bootstrap_factory is not None:
        try:
            return bootstrap_factory(settings, browser_factory=bf)
        except TypeError:
            return bootstrap_factory(settings)
    return Bootstrap(settings, browser_factory=bf)


async def check_public_v2_and_run(
    settings: Any, *, channel_factory: Any = None, bootstrap_factory: Any = None,
    vk_listener_factory: Any = None, browser_factory: Any = None,
    stop_event: asyncio.Event | None = None,
) -> bool:
    vk_credentials = _vk_binding_credentials(settings)
    if not settings.public_v2.enabled:
        return False
    from hunter.notify.telegram import TelegramChannel
    from hunter.notify.public_telegram_sender import PublicTelegramSender

    channel = (channel_factory or TelegramChannel)(settings)
    if not channel.enabled():
        raise RuntimeError("Public v2 requires a configured Telegram bot token and owner chat id")
    bs = _create_bootstrap_instance(settings, bootstrap_factory, browser_factory)
    tg_sender = PublicTelegramSender(bs, channel)
    tg_sender.start_heartbeat()
    bs.senders["telegram"] = tg_sender
    if vk_credentials:
        bs.senders["vk"] = _make_vk_sender(bs, vk_credentials[0])
    bs.owner_alert_sender = getattr(channel, "alert", None)
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    try:
        await bs.startup()
        listener = asyncio.create_task(
            channel.listen(db=bs.db, subscription_service=bs.subscription_service),
            name="public-v2-telegram-listener",
        )
        bs._tasks.append(listener)
        if vk_credentials:
            from hunter.application.vk_binding_listener import VkBindingListener
            from hunter.transport.vk_longpoll import VkLongPollClient

            token, binding_service = vk_credentials
            client = VkLongPollClient(settings.vk_group_id, token)
            vk_listener = (vk_listener_factory or VkBindingListener)(
                group_id=settings.vk_group_id,
                writer=bs.writer,
                binding_service=binding_service,
                client=client,
            )
            vk_task = asyncio.create_task(
                vk_listener.run(stop_event), name="public-v2-vk-binding-listener"
            )
            bs._tasks.append(vk_task)
        code = await bs.run_supervised(stop_event)
    finally:
        await bs.shutdown()
    if code != 0:
        raise RuntimeError(f"Public v2 bootstrap failed with code {code}")
    return True


def _make_vk_sender(bootstrap: Any, token: str) -> Any:
    from hunter.notify.public_vk_sender import PublicVkSender
    from hunter.notify.vk_delivery import VkDeliveryAdapter

    adapter = VkDeliveryAdapter(token)

    async def send_vk(delivery: Any) -> Any:
        return await PublicVkSender(bootstrap.conn_mgr, adapter, bootstrap.writer)(delivery)

    return send_vk


def _vk_binding_credentials(settings: Any) -> tuple[str, Any] | None:
    if not getattr(settings, "vk_binding_enabled", False):
        return None
    if not settings.public_v2.enabled:
        raise RuntimeError("VK binding listener requires public v2 runtime")
    token = getattr(settings, "vk_group_token", None)
    group_id = getattr(settings, "vk_group_id", None)
    secret = getattr(settings, "channel_binding_secret", None)
    if (
        token is None
        or not token.get_secret_value().strip()
        or group_id is None
        or group_id <= 0
        or not secret
    ):
        raise RuntimeError("VK binding listener requires group ID, group token, and binding secret")
    from hunter.application.channel_binding import ChannelBindingService

    return token.get_secret_value(), ChannelBindingService.from_settings(settings)


async def init_hunter_runtime(settings: Any) -> tuple[Any, Any, Any, Any, Any]:
    raise RuntimeError("Legacy v1 pipeline has been decommissioned. Please enable public_v2.")


def setup_signal_handlers() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        if stop.is_set():
            logger.warning("Force stopping hunter immediately...")
            os._exit(0)
        logger.info("Stopping hunter... (press Ctrl+C again to force exit)")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: _on_signal())
    return stop


async def shutdown_hunter_runtime(
    scheduler: Any,
    orch: Any,
    listen_task: asyncio.Task,
    server: Any,
    server_task: Any,
    browser: Any,
    db: Any,
) -> None:
    scheduler.shutdown(wait=False)
    try:
        await asyncio.wait_for(orch.stop_worker(), timeout=3.0)
    except Exception:
        pass
    listen_task.cancel()
    if server:
        server.should_exit = True
    if server_task:
        server_task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(
                browser.close(),
                db.close(),
                listen_task,
                return_exceptions=True,
            ),
            timeout=5.0,
        )
    except (TimeoutError, Exception):
        pass
    try:
        from hunter.core.executor import shutdown_cpu_executor

        shutdown_cpu_executor(wait=False)
    except Exception:
        pass
    logger.info("hunter stopped")


@app.command()
def status() -> None:
    asyncio.run(_status())


async def _status() -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    used = await db.today_requests()
    paused = await db.is_paused()
    circuit = await db.get_runtime("circuit_until")
    watches = load_watchlists()
    typer.echo(f"db: {settings.db_path}")
    typer.echo(f"paused: {paused}")
    typer.echo(f"circuit_until: {circuit}")
    typer.echo(f"requests today: {used} / {settings.request_budget.max_per_day}")
    typer.echo(f"notify_mode: {settings.notify_mode}")
    typer.echo(f"global_max_price: {settings.global_max_price or 'off'}")
    typer.echo(f"telegram: {'yes' if settings.telegram_bot_token else 'no'}")
    for w in watches:
        urls = "ok" if w.search_urls else "NO URL"
        poll_str = (
            f"{w.poll_seconds}s"
            if getattr(w, "poll_seconds", None)
            else f"{w.poll_minutes}m"
        )
        typer.echo(
            f"  - {w.id} [{w.category}] max={w.max_price} poll={poll_str} urls={urls}"
        )
    await db.close()


@app.command()
def pause() -> None:
    asyncio.run(_set_paused(True))


@app.command()
def resume() -> None:
    asyncio.run(_set_paused(False))


async def _set_paused(value: bool) -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    await db.set_runtime("paused", "1" if value else "0")
    await db.set_runtime("captcha", "0")
    flag = settings.data_dir / "pause.flag"
    captcha = settings.data_dir / "captcha.flag"
    if value:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text("paused\n")
        await db.set_runtime("paused", "1")
    else:
        flag.unlink(missing_ok=True)
        captcha.unlink(missing_ok=True)
        await db.clear_all_pauses()
        await db.set_runtime("coverage_trip", "")
    typer.echo("paused" if value else "resumed")
    await db.close()


@app.command()
def explain(avito_id: str) -> None:
    asyncio.run(_explain(avito_id))


async def _explain(avito_id: str) -> None:
    db = get_db()
    await db.connect()
    listing = await db.get_listing(avito_id)
    if not listing:
        typer.echo("not found")
        await db.close()
        raise typer.Exit(1)
    typer.echo(json.dumps(listing, ensure_ascii=False, indent=2, default=str))
    evaluation = await db.latest_evaluation(avito_id)
    if evaluation:
        typer.echo("--- latest evaluation ---")
        typer.echo(json.dumps(evaluation, ensure_ascii=False, indent=2, default=str))
    events = await db.events(avito_id)
    for event in events:
        typer.echo(f"{event['ts']} {event['kind']} {event['payload']}")
    await db.close()

