"""Clear only the confirmed failed identity; do not rotate or switch IP/session."""
from hunter.transport.recovery_identity import canonical_identity


def reset_fetcher_identity(fetcher, identity):
    from hunter.transport.http_fetch import DEFAULT_CHROME_USER_AGENT, DEFAULT_FIREFOX_USER_AGENT
    identity = canonical_identity(identity)
    if identity is None:
        raise ValueError("Explicit identity required for memory reset")
    a_clients = (fetcher.client, fetcher.curl_client)
    b_clients = (fetcher.curl_client_b,)
    clients, other = (a_clients, b_clients) if identity == "user_a" else (b_clients, a_clients)
    other_ids = {id(client) for client in other if client is not None}
    if any(id(client) in other_ids for client in clients if client is not None):
        raise RuntimeError("Shared client cannot be reset as a single identity")
    ua = fetcher.config.user_agent
    if identity == "user_b":
        ua = DEFAULT_CHROME_USER_AGENT if "Firefox" in ua else DEFAULT_FIREFOX_USER_AGENT
    for client in clients:
        if client is not None:
            client.cookies.clear()
            from hunter.transport.browser_headers import _default_browser_headers
            client.headers.clear()
            client.headers.update(_default_browser_headers(ua))
    if identity == "user_b":
        fetcher._user_b_ready = False
        fetcher._user_b_store_fence = None
