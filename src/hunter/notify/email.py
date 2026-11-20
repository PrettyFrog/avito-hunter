from __future__ import annotations

import smtplib
from email.message import EmailMessage

from hunter.core.config import get_settings
from hunter.core.models import NotificationEvent
from hunter.notify.base import Channel, format_event


class EmailChannel(Channel):
    name = "email"

    def enabled(self) -> bool:
        s = get_settings()
        return bool(s.smtp_host and s.smtp_from and s.smtp_to)

    async def send(self, event: NotificationEvent) -> bool:
        return await self.alert(format_event(event))

    async def alert(self, text: str) -> bool:
        s = get_settings()
        if not self.enabled():
            return False
        msg = EmailMessage()
        msg["Subject"] = "avito-hunter"
        msg["From"] = s.smtp_from
        msg["To"] = s.smtp_to
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if s.smtp_user:
                password = s.smtp_password.get_secret_value() if s.smtp_password else ""
                smtp.login(s.smtp_user, password)
            smtp.send_message(msg)
        return True
