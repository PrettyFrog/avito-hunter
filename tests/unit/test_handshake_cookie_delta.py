"""Transactional server cookies must survive recovery merging and disk persistence."""
import asyncio
import httpx
import pytest
from hunter.transport.qrator_handshake import perform_qrator_handshake
from hunter.transport.recovery_transport import merge_scoped_cookies


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_server_cookie_delta_updates_deletes_scopes_and_metadata(failure):
    async def reply(r):
        if r.method == 'POST':
            return httpx.Response(200, json='T' * 48, headers=[
                ('set-cookie', 'nonce=new; Path=/; Secure; HttpOnly; Max-Age=600'),
                ('set-cookie', 'obsolete=; Path=/; Max-Age=0'),
                ('set-cookie', 'scoped=special; Path=/special; Secure')], request=r)
        return httpx.Response(503 if failure else 200, headers={'set-cookie': 'pixel=ok; Path=/'}, request=r)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as c:
        c.cookies.set('nonce', 'old', domain='www.avito.ru', path='/')
        c.cookies.set('obsolete', 'old', domain='www.avito.ru', path='/')
        res = await perform_qrator_handshake(c, 'offline')
        if failure:
            assert not res.is_success
            assert c.cookies.get('nonce') == 'old' and c.cookies.get('obsolete') == 'old'
            assert c.cookies.get('pixel') is None
            return
        assert res.is_success and c.cookies.get('nonce') == 'new'
        assert c.cookies.get('obsolete') is None and c.cookies.get('pixel') == 'ok'
        base = [{'name': 'obsolete', 'value': 'old', 'domain': 'www.avito.ru', 'path': '/'}]
        enriched = merge_scoped_cookies(base, res.cookies)
        persisted = merge_scoped_cookies(base, enriched)
        assert not any(x['name'] == 'obsolete' for x in persisted)
        nonce = next(x for x in persisted if x['name'] == 'nonce')
        assert nonce['secure'] and nonce['httpOnly'] and nonce['expires'] > 0
        assert next(x for x in persisted if x['name'] == 'scoped')['path'] == '/special'


@pytest.mark.asyncio
async def test_cancel_and_concurrent_cookie_update_do_not_publish_delta():
    entered, release = asyncio.Event(), asyncio.Event()
    async def reply(r):
        entered.set()
        await release.wait()
        return httpx.Response(200, json='T'*48, headers={'set-cookie': 'nonce=staged; Path=/'}, request=r)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as c:
        task = asyncio.create_task(perform_qrator_handshake(c, 'offline'))
        await entered.wait()
        c.cookies.set('nonce', 'winner', domain='www.avito.ru', path='/')
        release.set()
        assert not (await task).is_success
        assert c.cookies.get('nonce') == 'winner' and c.cookies.get('ft') is None


pytest_plugins = ('tests.fixtures.reference_storage',)


@pytest.mark.asyncio
async def test_recovery_delta_survives_real_session_store(reference_storage):
    from hunter.transport.challenge import ChallengeHandoffService
    from hunter.transport.cookie_delta import CookieDelta
    from hunter.transport.session_store import SessionStore
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    slot = await repo.register_slot('delta-slot', 'direct')
    old = [{'name':'obsolete', 'value':'old', 'domain':'.avito.ru', 'path':'/'}]
    await store.save_session('delta-slot', 'direct', 1, 1, cookies=old)
    delta = CookieDelta([{'name':'fresh', 'value':'new', 'domain':'.avito.ru', 'path':'/special',
                          'expires':2000000000, 'secure':True, 'httpOnly':True}],
                        [('obsolete','.avito.ru','/')])
    enriched = merge_scoped_cookies(old, delta)
    service = ChallengeHandoffService(repo, store, allow_browser=False)
    assert await service._save_challenge_state(slot, None, enriched, client_identity='user_a')
    reopened = SessionStore(store.base_dir)
    reopened.bind_reference_backend(repo.writer)
    result = await reopened.load_session('delta-slot', expected_endpoint='direct')
    assert result is not None
    assert not any(c['name']=='obsolete' for c in result.cookies)
    fresh = next(c for c in result.cookies if c['name']=='fresh')
    assert fresh['path']=='/special' and fresh['expires']==2000000000
    assert fresh['secure'] and fresh['httpOnly']

    from hunter.transport.recovery_transport import apply_scoped_cookies_to_client
    async with httpx.AsyncClient() as restored:
        apply_scoped_cookies_to_client(restored, result.cookies)
        cookie = next(c for c in restored.cookies.jar if c.name == "fresh")
        assert cookie.secure and cookie.expires == 2000000000
        assert cookie.has_nonstandard_attr("HttpOnly")
