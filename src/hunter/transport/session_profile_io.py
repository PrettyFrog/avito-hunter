"""B-only hydration and persistence with a pre-warmup profile fence."""
from hunter.transport.session_identity import hydrate_identity
from hunter.transport.recovery_identity import canonical_identity, resolve_session_slot_id


async def load_identity_profile(fetcher, identity):
    identity = canonical_identity(identity)
    if identity is None:
        raise ValueError("Explicit identity required for profile reload")
    if fetcher.session_store is None:
        return 0
    sid = resolve_session_slot_id(fetcher.slot_id, identity)
    profile = await fetcher.session_store.load_session(sid, expected_endpoint=fetcher.endpoint or "")
    if not hydrate_identity(fetcher, identity, profile):
        return 0
    if identity == "user_b":
        fetcher._user_b_ready = bool(fetcher.curl_client_b is not None and len(fetcher.curl_client_b.cookies))
        fetcher._user_b_store_fence = (fetcher.endpoint, (profile.metadata.version, profile.metadata.generation + 1))
    return len(profile.cookies)


async def prepare_user_b_warmup(fetcher):
    store = fetcher.session_store
    if store is None:
        return None
    sid = f"{fetcher.slot_id}__user_b"
    profile = await store.load_session(sid)
    matching_endpoint = profile is None or profile.metadata.endpoint == (fetcher.endpoint or "")
    if not matching_endpoint and not fetcher.endpoint:
        from hunter.transport.session_store import EndpointMismatchError
        raise EndpointMismatchError("Direct fetcher cannot adopt proxy-bound profile")
    fence = ((profile.metadata.version, profile.metadata.generation + 1) if profile is not None
             else await store.next_save_fence(sid, only_absent=True))
    cache_key = (fetcher.endpoint, fence)
    if getattr(fetcher, "_user_b_store_fence", None) != cache_key:
        hydrate_identity(fetcher, "user_b", profile if matching_endpoint else None)
        fetcher._user_b_ready = False
        fetcher._user_b_store_fence = cache_key
    return fence


async def save_user_b_profile(fetcher, fence=None):
    store, client = fetcher.session_store, fetcher.curl_client_b
    if store is None or client is None:
        return
    cookies = []
    for cookie in client.cookies.jar:
        value = {"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path}
        if cookie.expires is not None:
            value["expires"] = cookie.expires
        cookies.append(value)
    if not cookies:
        return
    sid = f"{fetcher.slot_id}__user_b"
    version, generation = fence if fence is not None else await store.next_save_fence(sid)
    ua = client.headers.get("User-Agent")
    await store.save_session(slot_id=sid, endpoint=fetcher.endpoint or "", version=version,
                             generation=generation, cookies=cookies, storage_state={"user_agent": ua} if ua else {})
