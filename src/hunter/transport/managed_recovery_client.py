"""Own only recovery-created clients; borrowed poll clients keep their lifecycle."""
from contextlib import asynccontextmanager

from hunter.transport.recovery_transport import build_recovery_http_client


def _belongs_to_gateway(client, gateway) -> bool:
    from hunter.transport.metered_http import MeteredTransport
    if getattr(client, "gateway", None) is gateway:
        return True
    return isinstance(getattr(client, "_transport", None), MeteredTransport) and client._transport.gateway is gateway


@asynccontextmanager
async def managed_recovery_client(req, endpoint, gateway, groups):
    if req.http_client is not None:
        if gateway is not None and not _belongs_to_gateway(req.http_client, gateway):
            raise RuntimeError("Borrowed recovery client is outside the egress gateway")
        yield req.http_client
        return
    if gateway is None:
        yield None
        return
    group = groups.group_for(req.slot_id, endpoint)
    client, owned = build_recovery_http_client(
        endpoint, headers=req.headers, proxy_credentials=req.proxy_credentials,
        gateway=gateway, egress_group=group,
    )
    try:
        yield client
    finally:
        if owned:
            await client.aclose()
