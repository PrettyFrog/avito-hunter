"""Composition root and supervised shutdown for avito-hunter (S6 / T-489)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from pathlib import Path
from typing import Any

from hunter.application.capacity import CapacityAdmissionController
from hunter.application.cutover import CutoverManager
from hunter.application.dispatcher import DeliveryDispatcher
from hunter.application.public_poll_runtime import PublicPollRuntime
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.poll_policy import PollPolicy
from hunter.notify.max_delivery import MaxDeliveryAdapter
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import DeliveryRepository
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import ChallengeHandoffService
from hunter.transport.factory_bindings import bind_factory_stores
from hunter.transport.http_fetch import SlotFetchConfig, SlotHttpPool
from hunter.transport.session_store import SessionStore
from hunter.transport.proxy_credentials import ProxyCredentialStore

logger = logging.getLogger(__name__)
MAX_CONSECUTIVE_BACKGROUND_FAILURES = 3


class Bootstrap:
    """Composition root coordinating subsystem lifecycle and supervised shutdown."""

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        conn_mgr: StorageConnectionManager | None = None,
        writer: SQLiteWriterActor | None = None,
        senders: dict[str, Any] | None = None,
        http_pool: SlotHttpPool | None = None,
        browser_factory: Any = None,
    ) -> None:
        self.settings, self.db, self.conn_mgr, self.writer = settings, db, conn_mgr, writer
        self.senders = dict(senders or {})
        self.senders.setdefault("max", MaxDeliveryAdapter())
        self.http_pool, self.browser_factory = http_pool, browser_factory
        self.owner_alert_sender: Any = None

        # Subsystems
        self.slot_repo, self.delivery_repo = None, None
        self.session_store, self.proxy_credential_store = None, None
        self.dispatcher, self.cutover_mgr = None, None
        self.supervisor, self.challenge_service = None, None
        self.admission_ctrl, self.price_fetcher, self.egress_gateway = None, None, None
        self.price_watch_worker, self.public_poll_runtime = None, None
        self.channel_binding_service, self.poll_sequence = None, None
        self.workers: dict[str, Any] = {}
        self.pow_executor: Any = None

        self._tasks: list[asyncio.Task] = []
        self._price_watch_task: asyncio.Task | None = None
        self._fatal_error: BaseException | None = None
        self._is_started = False
        self._is_shutdown = False

    async def _init_storage(self, db_path: Path) -> None:
        if self.db is None:
            self.db = Database(db_path)
        if getattr(self.db, "_conn", None) is None:
            await self.db.connect()
        await self.db.migrate()

        if self.conn_mgr is None:
            self.conn_mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
        if self.writer is None:
            self.writer = SQLiteWriterActor(self.conn_mgr)
            await self.writer.start()

        self.slot_repo = SlotRepository(self.conn_mgr, self.writer, db=self.db)
        self.delivery_repo = DeliveryRepository(self.conn_mgr, self.writer)
        sessions_dir = self.settings.data_dir / "sessions" if hasattr(self.settings, "data_dir") else db_path.parent / "sessions"
        from hunter.transport.session_ownership import owned_session_directory
        self.session_store = SessionStore(base_dir=owned_session_directory(sessions_dir, db_path))
        self.session_store.bind_reference_backend(self.writer)
        self.proxy_credential_store = ProxyCredentialStore(sessions_dir.parent / "proxy_credentials")
        await bind_factory_stores(
            self.browser_factory, self.session_store, self.proxy_credential_store,
            getattr(self.settings.public_v2, "protected_proxy_auth", False),
        )

    async def _recover_state(self) -> None:
        if self.db and hasattr(self.db, "clear_all_pauses"):
            await self.db.clear_all_pauses()
        from hunter.storage.recovery_reconcile import reconcile_startup_state
        await reconcile_startup_state(self.session_store, self.writer, getattr(self.db, "path", None))
        if self.supervisor:
            await self.supervisor.check_quarantined_slots()

    async def _drain_recovery_loop(self) -> None:
        failures = 0
        while not self._is_shutdown:
            try:
                if self.cutover_mgr:
                    await self.cutover_mgr.recover_ready_drains()
                if self.supervisor:
                    recovered = await self.supervisor.check_quarantined_slots()
                    if recovered and self.public_poll_runtime:
                        await self.public_poll_runtime.refresh()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.exception("Tenant drain recovery failed")
                if failures >= MAX_CONSECUTIVE_BACKGROUND_FAILURES:
                    raise RuntimeError("Tenant drain recovery exceeded consecutive failure limit") from exc
            await asyncio.sleep(0.1)

    async def _price_watch_loop(self) -> None:
        failures = 0
        while not self._is_shutdown:
            try:
                await self.price_watch_worker.poll_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.exception("Price watch loop error")
                if failures >= MAX_CONSECUTIVE_BACKGROUND_FAILURES:
                    raise RuntimeError("Price watch exceeded consecutive failure limit") from exc
            await asyncio.sleep(self.price_watch_worker.defer_sec)

    async def _wire_services(self) -> None:
        if self.settings.public_v2.enabled:
            self.db._public_writer = self.writer
        self.admission_ctrl = CapacityAdmissionController(max_queries_per_slot=self.settings.public_v2.slot_capacity)
        self.cutover_mgr = CutoverManager()
        self.db.cutover_manager = self.cutover_mgr
        self.supervisor = SlotSupervisor(slot_repo=self.slot_repo, admission_ctrl=self.admission_ctrl, workers=self.workers)
        browser_allowed = getattr(self.settings.public_v2, "browser_fetch", False) if self.settings.public_v2.enabled else True
        effective_browser = self.browser_factory if browser_allowed else None
        if not self.pow_executor:
            from hunter.transport.pow_execution import CpuBoundExecutor, register_pow_executor
            self.pow_executor = CpuBoundExecutor()
            register_pow_executor(self.pow_executor)
        from hunter.application.egress_runtime import make_egress_gateway
        self.egress_gateway = make_egress_gateway(self.settings, self.writer)
        self.challenge_service = ChallengeHandoffService(
            slot_repo=self.slot_repo, session_store=self.session_store, browser_factory=effective_browser,
            allow_browser=browser_allowed, pow_executor=self.pow_executor,
            egress_gateway=self.egress_gateway, egress_groups=self.settings.egress_regulator,
        )
        self.dispatcher = DeliveryDispatcher(
            repo=self.delivery_repo, senders=self.senders, cutover_manager=self.cutover_mgr,
            enforce_user_status=self.settings.public_v2.enabled, revalidate_deliveries=self.settings.public_v2.enabled,
        )
        from hunter.application.subscriptions import SubscriptionService
        from hunter.storage.subscriptions import SubscriptionRepository
        self.subscription_repo = SubscriptionRepository(self.conn_mgr, self.writer)
        self.subscription_service = SubscriptionService(
            self.subscription_repo,
            auto_assign=self.settings.public_v2.enabled,
            max_queries_per_slot=self.settings.public_v2.slot_capacity,
        )
        if getattr(self.settings, "vk_binding_enabled", False):
            from hunter.application.channel_binding import ChannelBindingService
            self.channel_binding_service = ChannelBindingService.from_settings(self.settings)
            self.subscription_service.channel_binding_service = self.channel_binding_service
        self.db.subscription_service = self.subscription_service
        if self.settings.public_v2.enabled:
            from hunter.application.poll_sequence import PollSequenceAllocator
            self.poll_sequence = PollSequenceAllocator(self.writer)
        from hunter.application.price_wiring import wire_price_watch
        self.price_fetcher, self.price_watch_worker = wire_price_watch(self)
        await self._wire_public_poll_runtime()

    async def _wire_public_poll_runtime(self) -> None:
        if not self.settings.public_v2.enabled:
            return
        if self.egress_gateway and (self.settings.public_v2.browser_fetch or
                                    (self.http_pool is not None and getattr(self.http_pool, "gateway", None) is not self.egress_gateway)):
            raise RuntimeError("Public egress gateway cannot be bypassed by browser or external HTTP pool")
        if self.http_pool is None:
            base_url = getattr(self.settings, "avito_base_url", None)
            cfg = SlotFetchConfig(base_url=base_url) if base_url else None
            cred_store = self.proxy_credential_store if self.settings.public_v2.protected_proxy_auth else None
            browser_allowed = getattr(self.settings.public_v2, "browser_fetch", False)
            effective_browser = self.browser_factory if browser_allowed else None
            self.http_pool = SlotHttpPool(
                session_store=self.session_store,
                config=cfg,
                credential_store=cred_store,
                browser_factory=effective_browser,
                use_browser_fetch=browser_allowed,
                pow_executor=self.pow_executor,
                gateway=self.egress_gateway,
                egress_groups=self.settings.egress_regulator,
            )
        policy = PollPolicy.from_config(self.settings.public_v2)
        self.public_poll_runtime = PublicPollRuntime(
            self.conn_mgr, self.http_pool, self.ingest_observation, policy,
            is_paused=lambda: self.db.is_paused() if self.db else False,
            next_sequence=self.poll_sequence.next_sequence,
            slot_signal_handler=self.supervisor.handle_slot_signal if self.supervisor else None,
            challenge_service=self.challenge_service,
            require_auth=self.settings.public_v2.protected_proxy_auth,
        )
        self.public_poll_runtime.workers = self.workers
        self.subscription_service.on_query_assigned = self.public_poll_runtime.refresh_query
        if self.supervisor:
            self.supervisor.on_assignment_changed = self.public_poll_runtime.refresh_query

    async def ingest_observation(self, batch: Any, request_sequence: int = 1) -> Any:
        from hunter.storage.observation_tx import execute_observation_tx
        res = await self.writer.submit(
            lambda c: execute_observation_tx(c, batch, request_sequence, enforce_assignment=True)
        )
        if getattr(res, "deliveries_created", 0) > 0 and self.dispatcher:
            self.dispatcher.wake_up()
        return res

    async def startup(self) -> None:
        """Executes startup sequence: migrate -> recover -> start background tasks."""
        if self._is_started:
            return
        if self.settings.public_v2.enabled and not callable(self.senders.get("telegram")):
            raise RuntimeError("Public v2 requires a Telegram delivery sender before startup")

        db_path = Path(self.db.path) if self.db and hasattr(self.db, "path") else (
            Path(self.settings.db_path) if getattr(self.settings, "db_path", None) else Path("data/hunter.db")
        )
        await self._init_storage(db_path)
        await self._recover_state()
        await self._wire_services()

        if self.settings.public_v2.enabled and self.public_poll_runtime and isinstance(self.db, Database):
            await self.public_poll_runtime.refresh()
            self.workers.update(self.public_poll_runtime.workers)
            if self.supervisor:
                self.supervisor.workers = self.workers

        if self.settings.public_v2.enabled and self.dispatcher:
            from hunter.system.runtime_monitor import start_runtime_monitor

            self._tasks.append(start_runtime_monitor(self))
            self.dispatcher.start()
            if self.dispatcher._task:
                self._tasks.append(self.dispatcher._task)
            self._tasks.append(asyncio.create_task(self._drain_recovery_loop(), name="tenant_drain_recovery"))

        if self.settings.public_v2.enabled and getattr(self.settings.public_v2, "price_watch_enabled", False) and self.price_watch_worker:
            self._price_watch_task = asyncio.create_task(self._price_watch_loop(), name="price_watch_worker")
            self._tasks.append(self._price_watch_task)

        if self.settings.public_v2.enabled and self.db and hasattr(self.db, "cleanup_old_records"):
            self._tasks.append(asyncio.create_task(self._maintenance_loop(), name="db_maintenance"))
        self._is_started = True

    async def shutdown(self) -> None:
        """Executes shutdown protocol: stop -> drain -> close."""
        if self._is_shutdown:
            return
        self._is_shutdown = True

        # 1. Stop
        await self._stop_workers()

        if self.dispatcher:
            try:
                await self.dispatcher.stop()
            except Exception as exc:
                logger.warning("Error stopping dispatcher: %s", exc)

        # 2. Drain
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self.price_watch_worker:
            try:
                await self.price_watch_worker.release_leases()
            except Exception as exc:
                logger.warning("Error releasing price watch leases: %s", exc)

        # 3. Close
        if self.egress_gateway:
            self.egress_gateway.budget.close()
        for comp, meth, label in [
            (self.price_fetcher, "close", "price fetcher"),
            (self.http_pool, "close_all", "HTTP pool"),
            (self.writer, "stop", "writer actor"),
            (self.conn_mgr, "drain_and_close", "connection manager"),
            (self.db, "close", "DB"),
        ]:
            if comp and hasattr(comp, meth):
                try:
                    await getattr(comp, meth)()
                except Exception as exc:
                    logger.warning("Error closing %s: %s", label, exc)
        if self.pow_executor:
            try:
                self.pow_executor.shutdown(wait=False)
            except Exception as exc:
                logger.warning("Error closing pow executor: %s", exc)
            finally:
                from hunter.transport.pow_execution import unregister_pow_executor
                unregister_pow_executor(self.pow_executor)
                self.pow_executor = None

    async def _stop_workers(self) -> None:
        for target in [self.public_poll_runtime, *self.workers.values()]:
            if target and hasattr(target, "stop"):
                try:
                    await target.stop()
                except Exception as exc:
                    logger.warning("Error stopping worker: %s", exc)
        if self.challenge_service: await self.challenge_service.close()
    async def run_supervised(self, stop_event: asyncio.Event) -> int:
        """Supervises background execution; ordinary slot errors isolated, fatal task returns 1."""
        wait_stop: asyncio.Task[bool] | None = None
        try:
            await self.startup()
            wait_stop = asyncio.create_task(stop_event.wait(), name="stop_waiter")
            wait_set = [wait_stop, *self._tasks]
            if self.writer and self.writer._worker_task:
                wait_set.append(self.writer._worker_task)
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            for t in done:
                if t is wait_stop:
                    continue
                exc = (
                    RuntimeError(f"Supervised task {t.get_name()} was cancelled")
                    if t.cancelled()
                    else t.exception()
                )
                if exc is None:
                    exc = RuntimeError(f"Supervised task {t.get_name()} exited unexpectedly")
                self._fatal_error = exc
                logger.error("Fatal task terminated bootstrap: %s", exc)
                return 1
            return 0
        finally:
            if wait_stop is not None and not wait_stop.done():
                wait_stop.cancel()
            if wait_stop is not None:
                await asyncio.gather(wait_stop, return_exceptions=True)
            await self.shutdown()

    async def run_maintenance(self) -> dict[str, int]:
        """Runs periodic retention cleanup for historical logs, jobs, and terminal deliveries."""
        if self.db and hasattr(self.db, "cleanup_old_records"):
            return await self.db.cleanup_old_records()
        return {}

    async def _maintenance_loop(self) -> None:
        while not self._is_shutdown:
            try:
                await asyncio.sleep(86400)
                await self.run_maintenance()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Error in background maintenance loop: %s", exc)

    async def get_health(self) -> dict[str, Any]:
        """Aggregate health metrics separating search budget from price watch extras."""
        from hunter.application.workload_estimate import estimate_workload

        pwatch = await self.price_watch_worker.get_metrics() if self.price_watch_worker else {"enabled": False}
        workload = 0
        if self.settings.public_v2.enabled:
            if not self.conn_mgr:
                raise RuntimeError("Storage is unavailable for workload estimation")
            reader = await self.conn_mgr.open_read_connection()
            try:
                plan = await estimate_workload(reader, self.settings.public_v2)
                workload = plan.search_requests_per_day
            finally:
                await self.conn_mgr.close_reader(reader)
        return {
            "public_v2_enabled": self.settings.public_v2.enabled,
            "search_workload_requests_per_day": workload,
            "search_workload_is_estimate": True,
            "price_watch": pwatch,
        }


HunterBootstrap = Bootstrap

