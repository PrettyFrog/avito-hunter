from __future__ import annotations

from hunter.core.config import get_settings
from hunter.notify.base import Notifier
from hunter.notify.email import EmailChannel
from hunter.notify.ntfy import NtfyChannel
from hunter.notify.telegram import TelegramChannel


def build_notifier() -> Notifier:
    get_settings()
    channels = []
    tg = TelegramChannel()
    if tg.enabled():
        channels.append(tg)
    ntfy = NtfyChannel()
    if ntfy.enabled():
        channels.append(ntfy)
    email = EmailChannel()
    if email.enabled():
        channels.append(email)
    return Notifier(channels)
