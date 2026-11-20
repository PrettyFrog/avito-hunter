"""Construct one shared egress admission owner for the public runtime."""
from hunter.core.config import Settings
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.request_budget import RequestBudget
from hunter.storage.request_cooldown import RequestCooldownStore
from hunter.transport.metered_http import RequestGateway


def make_egress_gateway(settings: Settings, writer) -> RequestGateway | None:
    config = settings.egress_regulator
    if config.mode == "off":
        return None
    policy = PollPolicy.from_config(settings.public_v2)
    budget = RequestBudget(
        enforce=config.mode == "enforce",
        spacing_provider=lambda: config.spacing_for(is_day=policy.is_day()),
        shared_upstreams=False,
        max_active=config.max_active,
        max_cooldown_sec=config.max_cooldown_sec,
    )
    return RequestGateway(budget, cooldown_store=RequestCooldownStore(writer),
                          max_waiters=config.max_waiters)
