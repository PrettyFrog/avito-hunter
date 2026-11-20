"""Validate persisted browser identity before hydrating a pinned transport."""
import re
from loguru import logger
from hunter.transport.browser_headers import _default_browser_headers
from hunter.transport.recovery_transport import apply_scoped_cookies_to_client


def browser_signature(ua):
    if not isinstance(ua, str): return None
    match = re.search(r'(Firefox|Chrome)/(\d+)\.', ua or '')
    platform = next((p for p, marker in [('Linux','Linux'),('Windows','Windows NT'),('macOS','Macintosh')] if marker in (ua or '')), None)
    return (match[1], int(match[2]), platform) if match and platform else None


def identity_user_agent(fetcher, identity):
    from hunter.transport.http_fetch import DEFAULT_CHROME_USER_AGENT, DEFAULT_FIREFOX_USER_AGENT
    if identity=='user_a': return fetcher.config.user_agent
    return DEFAULT_CHROME_USER_AGENT if 'Firefox' in fetcher.config.user_agent else DEFAULT_FIREFOX_USER_AGENT


def hydrate_identity(fetcher, identity, profile):
    expected = identity_user_agent(fetcher, identity)
    saved = (profile.storage_state or {}).get('user_agent') if profile else None
    fetcher.reset_identity_in_memory(identity)
    if profile is None:
        return False
    if saved and browser_signature(saved) != browser_signature(expected):
        logger.warning('Session browser profile rejected: identity={}, expected={}, saved={}',
                       identity, browser_signature(expected), browser_signature(saved))
        return False
    clients = (fetcher.client,fetcher.curl_client) if identity=='user_a' else (fetcher.curl_client_b,)
    for client in clients:
        if client is not None:
            client.headers.clear()
            client.headers.update(_default_browser_headers(saved or expected))
            if language := (profile.storage_state or {}).get('accept_language'):
                client.headers['Accept-Language'] = language
            apply_scoped_cookies_to_client(client, profile.cookies)
    if identity=='user_b':
        fetcher._user_b_ready = bool(fetcher.curl_client_b is not None and len(fetcher.curl_client_b.cookies))
    return True


def validate_native_user_agent(ua):
    from hunter.transport.http_fetch import DEFAULT_CHROME_USER_AGENT, DEFAULT_FIREFOX_USER_AGENT
    if browser_signature(ua) not in {browser_signature(DEFAULT_CHROME_USER_AGENT), browser_signature(DEFAULT_FIREFOX_USER_AGENT)}:
        raise ValueError('User-Agent is incompatible with pinned native browser presets')
