"""Lazy facade proxy for route modules, preserving runtime patch points."""

def __getattr__(name: str):
    from hunter.notify import telegram_router

    return getattr(telegram_router, name)
