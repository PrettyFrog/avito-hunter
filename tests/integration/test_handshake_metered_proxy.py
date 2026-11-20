"""Real native transport through a loopback HTTP proxy; no external traffic."""
import asyncio
import pytest
from aiohttp import web
from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import RequestGateway
from hunter.transport.metered_curl import MeteredCurlSession
from hunter.transport.qrator_handshake import perform_qrator_handshake


async def _proxy_server(entered, release, seen):
    async def proxy(r):
        seen.append((r.path, dict(r.cookies), r.headers.get('Proxy-Authorization'), r.headers.get('User-Agent')))
        if r.path == '/web/2/ft':
            entered.set()
            await release.wait()
            return web.json_response('T' * 48, headers=[
                ('Set-Cookie', 'server_nonce=new; Domain=.avito.ru; Path=/; Max-Age=600; HttpOnly'),
                ('Set-Cookie', 'obsolete=; Domain=.avito.ru; Path=/; Max-Age=0')])
        return web.Response(text='ok')
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'cancel', 'close'])
@pytest.mark.parametrize('enforce', [False, True])
async def test_staged_metered_proxy_preserves_owner_and_native_lifecycle(mode, enforce, monkeypatch):
    import hunter.transport.qrator_handshake as hs
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    runner, port = await _proxy_server(entered, release, seen)
    orig = hs.scoped_http_call
    monkeypatch.setattr(hs, 'scoped_http_call', lambda cl,p,m,u,**k: orig(cl,p,m,u.replace('https://','http://'),**k))
    budget = RequestBudget(enforce=enforce)
    client = MeteredCurlSession(gateway=RequestGateway(budget), egress_group='test', generation=1,
                               trust_env=False, proxy=f'http://127.0.0.1:{port}', proxy_auth=('test','secret'),
                               headers={'User-Agent':'offline-profile'}, impersonate='chrome131', emit=lambda e: None)
    client.cookies.set('f','old-f',domain='.avito.ru',path='/')
    client.cookies.set('ft','old-ft',domain='.avito.ru',path='/')
    client.cookies.set('obsolete','old',domain='.avito.ru',path='/')
    task = asyncio.create_task(perform_qrator_handshake(client,'offline'))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if enforce:
            from hunter.transport.metered_http import HttpDeferred
            with pytest.raises(HttpDeferred): await client.get('http://www.avito.ru/parallel')
            assert client.cookies.get('f')=='old-f' and client.cookies.get('ft')=='old-ft'
        else:
            await client.get('http://www.avito.ru/parallel')
            parallel = next(x for x in seen if x[0]=='/parallel')
            assert parallel[1]['f']=='old-f' and parallel[1]['ft']=='old-ft'
        if mode != "success":
            if mode == "close": await asyncio.wait_for(client.close(), 3)
            else: task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert client.cookies.get('f')=='old-f' and client.cookies.get('server_nonce') is None
        else:
            release.set()
            result = await asyncio.wait_for(task, 3)
            assert result.is_success and client.cookies.get('server_nonce')=='new'
            assert client.cookies.get('obsolete') is None
            assert any(c['name']=='server_nonce' and c['expires'] for c in result.cookies)
        release.set()
        if mode != 'close':
            assert (await client.get('http://www.avito.ru/after')).status_code==200
        assert all(x[2]=='Basic dGVzdDpzZWNyZXQ=' and x[3]=='offline-profile' for x in seen)
        assert not client._requests and not client._native_hooks
        assert budget.snapshot('test','avito')['active']==0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await runner.cleanup()
