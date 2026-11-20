"""Saved UA must never be applied to an incompatible native transport."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from hunter.transport.http_fetch import HttpSlotFetcher, DEFAULT_CHROME_USER_AGENT, DEFAULT_FIREFOX_USER_AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['startup', 'reload', 'warmup'])
@pytest.mark.parametrize('saved_ua', [DEFAULT_CHROME_USER_AGENT.replace('131.', '133.'), DEFAULT_FIREFOX_USER_AGENT, DEFAULT_CHROME_USER_AGENT.replace('X11; Linux x86_64','Windows NT 10.0; Win64; x64')])
async def test_incompatible_b_profile_is_not_loaded(entry, saved_ua):
    from hunter.transport.session_profile_io import prepare_user_b_warmup
    profile = SimpleNamespace(cookies=[{'name':'u','value':'incompatible','domain':'.avito.ru','path':'/'}],
                              storage_state={'user_agent':saved_ua},
                              metadata=SimpleNamespace(endpoint='direct',version=1,generation=1))
    store = SimpleNamespace(load_session=AsyncMock(return_value=profile))
    fetcher = HttpSlotFetcher('browser-versions','direct',session_store=store)
    try:
        if entry=='startup': await fetcher.load_cookies_from_store()
        elif entry=='reload': await fetcher.load_identity_from_store('user_b')
        else: await prepare_user_b_warmup(fetcher)
        assert fetcher.curl_client_b.cookies.get('u') is None
        assert fetcher.curl_client_b.headers['User-Agent']==DEFAULT_CHROME_USER_AGENT
        assert '"Chromium";v="131"' in fetcher.curl_client_b.headers['sec-ch-ua']
        assert fetcher.curl_client_b.impersonate=='chrome131'
        assert not fetcher._user_b_ready
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_compatible_profile_keeps_cookie_and_headers_aligned():
    profile = SimpleNamespace(cookies=[{'name':'u','value':'valid','domain':'.avito.ru','path':'/'}],
                              storage_state={'user_agent':DEFAULT_CHROME_USER_AGENT},
                              metadata=SimpleNamespace(endpoint='direct',version=1,generation=1))
    fetcher = HttpSlotFetcher('browser-valid','direct',session_store=SimpleNamespace(load_session=AsyncMock(return_value=profile)))
    try:
        assert await fetcher.load_identity_from_store('user_b')==1
        assert fetcher.curl_client_b.cookies.get('u')=='valid'
        assert fetcher.curl_client_b.impersonate=='chrome131'
        assert fetcher.curl_client_b.headers['User-Agent']==DEFAULT_CHROME_USER_AGENT
        assert '"Chromium";v="131"' in fetcher.curl_client_b.headers['sec-ch-ua']
        assert fetcher._user_b_ready
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_missing_reverse_b_profile_resets_firefox_without_chrome_hints():
    from hunter.transport.http_fetch import SlotFetchConfig
    fetcher = HttpSlotFetcher('browser-reverse','direct',config=SlotFetchConfig(user_agent=DEFAULT_CHROME_USER_AGENT),
                              session_store=SimpleNamespace(load_session=AsyncMock(return_value=None)))
    try:
        await fetcher.load_cookies_from_store()
        assert fetcher.curl_client_b.impersonate=='firefox133'
        assert fetcher.curl_client_b.headers['User-Agent']==DEFAULT_FIREFOX_USER_AGENT
        assert 'sec-ch-ua' not in fetcher.curl_client_b.headers
    finally:
        await fetcher.close()


def test_incompatible_config_is_rejected_before_creating_clients():
    from hunter.transport.http_fetch import SlotFetchConfig
    with pytest.raises(ValueError,match='incompatible'):
        HttpSlotFetcher('bad-config','direct',config=SlotFetchConfig(user_agent=DEFAULT_CHROME_USER_AGENT.replace('131.','133.')))
