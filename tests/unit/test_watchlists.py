from hunter.core.config import load_watchlists, project_root


def test_multiple_watchlists_and_max_price() -> None:
    items = load_watchlists(project_root() / "config" / "watchlists")
    assert len(items) >= 2
    for item in items:
        assert item.id
        assert item.category in {"headphones", "laptops"}
        assert item.max_price > 0
        assert item.poll_minutes > 0
