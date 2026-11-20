"""Owned native session: preflight before connect, admission before each HTTP hop."""
import asyncio
from contextvars import ContextVar
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException, SessionClosed
from loguru import logger

from hunter.transport.metered_http import RequestGateway, upstream_for_host
from hunter.transport.native_admission import NativeHopAdmission, NativePrerequisiteHook
from hunter.transport.native_lifecycle import drain_owned
from hunter.transport.observed_curl import ObservedCurlSession
from hunter.transport.paced_native import paced_native_request


class _Transfer:
    def __init__(self, admission: NativeHopAdmission):
        self.admission = admission
        self.curl = None
        self.finished = asyncio.Event()


class MeteredCurlSession(ObservedCurlSession):
    def __init__(self, *, gateway: RequestGateway, egress_group: str | None,
                 generation: int, request_timeout_sec: float = 25, **kwargs):
        super().__init__(**kwargs)
        self.gateway, self.egress_group, self.generation = gateway, egress_group, generation
        self.request_timeout_sec = request_timeout_sec
        self._current: ContextVar[_Transfer | None] = ContextVar("native_transfer", default=None)
        self._native_hooks = {}
        self._requests = set()
        self._closing = False
        self._close_task = None
        self._flushes = set()
        self._flush_error = None

    async def _request_once(self, method, url, **kwargs):
        if self._closing:
            raise SessionClosed("Session is closing")
        if self._flush_error is not None:
            raise self._flush_error
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            if self.gateway.budget.enforce and self.gateway.budget.has_spacing:
                return await paced_native_request(self, method, url, kwargs, self._metered_once)
            return await self._metered_once(method, url, **kwargs)
        finally:
            self._requests.discard(task)

    async def _metered_once(self, method, url, *, admission_deadline=None, wait_for_window=False, **kwargs):
        upstream = upstream_for_host(urlsplit(str(url)).hostname or "")
        deadline = admission_deadline if admission_deadline is not None else self.gateway.budget.clock() + self.request_timeout_sec
        if not self.gateway.budget.shared_upstreams:
            await self.gateway.restore(egress_group=self.egress_group, upstream="other" if upstream == "avito" else "avito")
        permit = await self.gateway.acquire(egress_group=self.egress_group, upstream=upstream, deadline=deadline, wait=wait_for_window)
        gate = NativeHopAdmission(self.gateway.budget, permit, egress_group=self.egress_group,
                                  upstream=upstream, deadline=deadline)
        transfer = _Transfer(gate)
        token = self._current.set(transfer)
        try:
            try:
                response = await super()._request_once(method, url, **kwargs)
            except RequestException:
                if gate.failure is not None:
                    raise gate.failure
                raise
            if gate.failure is not None:
                raise gate.failure
            return response
        except BaseException:
            await self._abort(transfer)
            raise
        finally:
            self._current.reset(token)
            if transfer.curl is None:
                gate.close()
            if self.gateway.cooldown_store is not None:
                try:
                    await drain_owned(gate.persist_cooldown(self.gateway.cooldown_store, generation=self.generation))
                except BaseException:
                    await self._abort(transfer)
                    raise

    async def pop_curl(self):
        transfer = self._current.get()
        if transfer is None:
            raise RuntimeError("Native request has no admission context")
        curl = await super().pop_curl()
        transfer.curl = curl
        try:
            hook = NativePrerequisiteHook(curl, transfer.admission, observe_headers=True, resolve_upstream=
                lambda url: upstream_for_host(urlsplit(url).hostname or ""))
            self._native_hooks[curl] = (transfer, hook)
            return curl
        except BaseException:
            self.release_curl(curl)
            raise

    def release_curl(self, curl):
        # Keep the hook strongly owned until the superclass finishes native reset.
        owned = self._native_hooks.pop(curl, None)
        try:
            super().release_curl(curl)
        finally:
            if owned is not None:
                owned[1].release()
                owned[0].admission.close()
                owned[0].finished.set()
                self._queue_flush(owned[0].admission)

    def _queue_flush(self, admission):
        if self.gateway.cooldown_store is not None and admission.needs_flush:
            task = asyncio.create_task(admission.persist_cooldown(
                self.gateway.cooldown_store, generation=self.generation))
            self._flushes.add(task)
            task.add_done_callback(self._flush_finished)

    def _flush_finished(self, task):
        self._flushes.discard(task)
        error = RuntimeError("Cooldown persistence cancelled") if task.cancelled() else task.exception()
        if error is not None:
            self._flush_error = error
            self.gateway.budget.close()
            logger.error("Native cooldown persistence failed ({})", type(error).__name__)

    async def _abort(self, transfer):
        await drain_owned(self._abort_owned(transfer))

    async def _abort_owned(self, transfer):
        curl = transfer.curl
        owned = self._native_hooks.get(curl)
        if owned is not None and owned[0] is transfer:
            if curl in self.acurl._curl2future:
                self.acurl.remove_handle(curl)
            await transfer.finished.wait()
        transfer.admission.close()

    async def close(self):
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_owned())
        await drain_owned(self._close_task)

    async def _close_owned(self):
        pending = list(self._requests)
        for task in pending:
            task.cancel()
        for transfer, _ in list(self._native_hooks.values()):
            await self._abort(transfer)
        await asyncio.gather(*pending, return_exceptions=True)
        await super().close()
        await asyncio.gather(*list(self._flushes), return_exceptions=True)
        if self._flush_error is not None:
            raise self._flush_error
