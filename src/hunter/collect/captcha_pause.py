from __future__ import annotations

import asyncio
import re

from loguru import logger

from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.notify.base import Notifier

CAPTCHA_URL_HINTS = (
    "/blocked",
    "/firewall",
    "/show/captcha",
    "/ip_ban",
)

CAPTCHA_TITLE_HINTS = (
    "доступ ограничен",
    "доступ временно заблокирован",
    "подтвердите, что вы не робот",
    "проверка безопасности",
    "подозрительная активность",
    "проблема с ip",
    "слишком много запросов",
)

CAPTCHA_DOM_HINTS = (
    'action="/firewall"',
    'class="firewall-',
    'id="smartcaptcha"',
    'class="smartcaptcha"',
    "smartcaptcha",
    "geetest_holder",
    "подтвердите, что вы не робот",
    "доступ с вашего ip",
    "проблема с ip-адресом",
    "слишком много запросов",
    "пройдите проверку человека",
)


def looks_like_captcha(html: str, url: str | None = "") -> bool:
    url_lower = (url or "").lower()
    if any(hint in url_lower for hint in CAPTCHA_URL_HINTS):
        return True

    html_lower = html.lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_lower, re.DOTALL)
    if title_match:
        title = title_match.group(1)
        if any(hint in title for hint in CAPTCHA_TITLE_HINTS):
            return True

    return any(hint in html_lower for hint in CAPTCHA_DOM_HINTS)



async def wait_for_human(db: Database, notifier: Notifier | None) -> None:
    settings = get_settings()
    flag = settings.data_dir / "captcha.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("solve the captcha in the visible Chrome window, then delete this file\n")
    # Publish the captcha reason before the global pause becomes observable.
    # Pause broadcasting may yield, and workers must never see a captcha pause
    # without its corresponding durable reason.
    await db.set_runtime("captcha", "1")
    from hunter.notify.broadcast import set_global_pause_with_broadcast
    await set_global_pause_with_broadcast(db, paused=True)
    logger.error("captcha/block detected; solve it in Chrome, then: hunter resume")
    if notifier:
        await notifier.alert(
            "⚠️ **Обнаружена капча или блокировка IP на Авито!**\n"
            "Работа бота полностью приостановлена.\n"
            "Решите проверку в браузере Chrome и отправьте команду `/resume` в этот чат "
            "(или выполните `hunter resume` на сервере)."
        )
    while flag.exists() or await db.get_runtime("captcha") == "1" or await db.is_paused():
        await asyncio.sleep(15)
    await set_global_pause_with_broadcast(db, paused=False)
    await db.set_runtime("captcha", "0")
    logger.info("captcha pause cleared, continuing")
