"""Keep price watcher construction in the composition root's owned lifecycle."""
from hunter.application.price_watch_worker import PriceWatchWorker
from hunter.transport.price_fetch import PriceFetchAdapter, make_price_client


def wire_price_watch(bootstrap, *, client=None):
    fetcher = PriceFetchAdapter(make_price_client(bootstrap.egress_gateway, client))
    worker = PriceWatchWorker(
        bootstrap.writer, fetcher=fetcher,
        enabled=getattr(bootstrap.settings.public_v2, "price_watch_enabled", False),
        spare_capacity=lambda: getattr(bootstrap.admission_ctrl, "has_spare_capacity", lambda: True)(),
        next_sequence=bootstrap.poll_sequence.next_sequence if bootstrap.poll_sequence else None,
    )
    return fetcher, worker
