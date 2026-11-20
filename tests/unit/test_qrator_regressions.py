import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import httpx
import pytest
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.qrator_codec import DEFAULT_BASE_S, decode_field, encode_field
from hunter.transport.qrator_handshake import _parse_retry_after, perform_qrator_handshake

pytest_plugins = ('tests.fixtures.reference_storage',)

def test_retry_after_seconds_and_http_date():
    assert _parse_retry_after({'Retry-After': '45'}) == 45
    val = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10), usegmt=True)
    assert 590 <= _parse_retry_after({'Retry-After': val}) <= 600

@pytest.mark.asyncio
@pytest.mark.parametrize('fail_stage', ['ft_403', 'pixel_500', 'pixel_501', 'pixel_503', 'pixel_599', 'pixel_net'])
async def test_handshake_failure_preserves_cookies(fail_stage):
    async def respond(r):
        if r.url.path == '/web/2/ft':
            if fail_stage == 'ft_403': return httpx.Response(403, request=r)
            return httpx.Response(200, json='T' * 48, headers={'Set-Cookie': 'temp=val; Path=/'}, request=r)
        if fail_stage == 'pixel_net': raise httpx.ConnectError('offline', request=r)
        return httpx.Response(int(fail_stage.split('_')[1]), request=r)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        c.cookies.set('f', 'old-f', domain='.avito.ru', path='/')
        c.cookies.set('ft', 'old-ft', domain='.avito.ru', path='/')
        res = await perform_qrator_handshake(c, 'test')
        assert not res.is_success and c.cookies.get('f') == 'old-f' and c.cookies.get('ft') == 'old-ft'
        assert c.cookies.get('temp') is None

@pytest.mark.asyncio
async def test_wire_s_and_stable_canvas_hash():
    values, bodies = [], []
    async def respond(r):
        bodies.append(r.content)
        val = r.content.decode('ascii').split('name="f"\r\n\r\n', 1)[1].split('\r\n', 1)[0]
        values.append(decode_field(val, 'f').split(';')[106])
        return httpx.Response(403, request=r)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        f1, f2 = await perform_qrator_handshake(c, 'test'), await perform_qrator_handshake(c, 'test')
        assert not f1.is_success and not f2.is_success and len(values) == 2 and values[0] == values[1]
    enc = encode_field(DEFAULT_BASE_S, 's')
    assert len(enc) == 176 and all(ch in '0123456789abcdef' for ch in enc) and enc.encode() in bodies[0]

@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['f', 'session'])
async def test_concurrent_cookie_change_is_not_overwritten(name):
    entered, release = asyncio.Event(), asyncio.Event()
    async def respond(r):
        if r.method == 'POST':
            entered.set()
            await release.wait()
        return httpx.Response(200, json='T' * 48, request=r)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        t = asyncio.create_task(perform_qrator_handshake(c, 'test'))
        await entered.wait()
        c.cookies.set(name, 'concurrent', domain='.avito.ru', path='/')
        release.set()
        res = await t
        assert not res.is_success and c.cookies.get(name) == 'concurrent' and c.cookies.get('ft') is None

@pytest.mark.asyncio
async def test_cancelled_handshake_preserves_cookie_jar():
    ev = asyncio.Event()
    async def respond(r):
        ev.set()
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        c.cookies.set('f', 'old-f', domain='.avito.ru', path='/')
        t = asyncio.create_task(perform_qrator_handshake(c, 'test'))
        await ev.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError): await t
        assert c.cookies.get('f') == 'old-f'

@pytest.mark.asyncio
async def test_staging_preserves_request_hooks_and_borrowed_transport():
    seen = []
    async def respond(r): return httpx.Response(200, json='T' * 48, request=r)
    async def inspect(r): seen.append(r.url.path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), event_hooks={'request': [inspect]}) as c:
        assert (await perform_qrator_handshake(c, 'test')).is_success
        assert seen == ['/web/2/ft', '/web/1/u'] and (await c.get('https://www.avito.ru/')).status_code == 200

@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 403])
async def test_native_curl_stages_cookies(status, monkeypatch):
    from aiohttp import web
    from curl_cffi import CurlOpt
    from hunter.transport.observed_curl import ObservedCurlSession
    import hunter.transport.qrator_handshake as hs
    reqs = []
    async def h(r):
        reqs.append((r.path, dict(r.cookies)))
        return web.json_response('T' * 48, status=status) if r.path == '/web/2/ft' else web.Response(status=200)
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    orig = hs.scoped_http_call
    monkeypatch.setattr(hs, 'scoped_http_call', lambda cl, p, m, u, **k: orig(cl, p, m, u.replace('https://www.avito.ru', f'http://www.avito.ru:{port}'), **k))
    c = ObservedCurlSession(trust_env=False, curl_options={CurlOpt.RESOLVE: [f'www.avito.ru:{port}:127.0.0.1']})
    c.cookies.set('f', 'old-f', domain='.avito.ru', path='/')
    c.cookies.set('ft', 'old-ft', domain='.avito.ru', path='/')
    try:
        res = await perform_qrator_handshake(c, 'test')
        assert res.is_success is (status == 200) and reqs[0][1]['f'].startswith('5.')
        assert (c.cookies.get('f') == 'old-f') if status == 403 else (c.cookies.get('f').startswith('5.') and reqs[1][1]['ft'] == 'T' * 48)
    finally:
        await c.close()
        await runner.cleanup()

@pytest.mark.asyncio
async def test_native_curl_does_not_expose_staged_cookies_to_parallel_request(monkeypatch):
    from aiohttp import web
    from curl_cffi import CurlOpt
    from hunter.transport.observed_curl import ObservedCurlSession
    import hunter.transport.qrator_handshake as hs
    entered, release = asyncio.Event(), asyncio.Event()
    seen = {}
    async def h(r):
        seen[r.path] = dict(r.cookies)
        if r.path == '/web/2/ft':
            entered.set()
            await release.wait()
            return web.json_response('T' * 48)
        return web.Response(status=200)
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    orig = hs.scoped_http_call
    monkeypatch.setattr(hs, 'scoped_http_call', lambda cl, p, m, u, **k: orig(cl, p, m, u.replace('https://www.avito.ru', f'http://www.avito.ru:{port}'), **k))
    c = ObservedCurlSession(trust_env=False, curl_options={CurlOpt.RESOLVE: [f'www.avito.ru:{port}:127.0.0.1']})
    c.cookies.set('f', 'old-f', domain='.avito.ru', path='/')
    c.cookies.set('ft', 'old-ft', domain='.avito.ru', path='/')
    try:
        task = asyncio.create_task(perform_qrator_handshake(c, 'test'))
        await entered.wait()
        await c.get(f'http://www.avito.ru:{port}/parallel')
        release.set()
        await task
        assert seen['/parallel']['f'] == 'old-f' and seen['/parallel']['ft'] == 'old-ft'
    finally:
        release.set()
        await c.close()
        await runner.cleanup()

@pytest.mark.asyncio
@pytest.mark.parametrize('deferred', [False, True])
async def test_warmup_rejects_failed_handshake(deferred, monkeypatch):
    import hunter.transport.qrator_handshake as hs
    from hunter.transport.session_warmup import perform_session_warmup
    class Cl:
        cookies = httpx.Cookies()
        async def get(self, url, **kw): return type('R', (), {'status_code': 200, 'text': '<div data-marker="catalog-serp"><div data-marker="item">x</div></div>'})()
    async def fail(*a, **kw):
        if deferred: raise HttpDeferred('cooldown', 100)
        return hs.QratorHandshakeResult(hs.HandshakeOutcome.ERROR, False)
    monkeypatch.setattr(hs, 'perform_qrator_handshake', fail)
    if deferred:
        with pytest.raises(HttpDeferred): await perform_session_warmup(Cl(), 'https://www.avito.ru/')
    else:
        assert not (await perform_session_warmup(Cl(), 'https://www.avito.ru/')).is_ready

@pytest.mark.asyncio
async def test_failed_warmup_does_not_persist_user_b(reference_storage):
    from hunter.transport.http_fetch import HttpSlotFetcher
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    f = HttpSlotFetcher('qrator-reg', 'direct', transport=httpx.MockTransport(lambda r: httpx.Response(403 if r.method == 'POST' else 200, text='<div data-marker="catalog-serp"><div data-marker="item">x</div></div>', request=r)))
    c = httpx.AsyncClient()
    f.session_store, f.curl_client_b, f._user_b_ready = store, c, False
    try:
        assert not await f._warmup_b_if_needed(c, 'https://www.avito.ru/')
        assert not f._user_b_ready and not c.cookies and await store.load_session('qrator-reg__user_b', expected_endpoint='direct') is None
    finally:
        f.curl_client_b = None
        await c.aclose()
        await f.close()
