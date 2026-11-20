"""Automated button and dialogue state crawler for Avito Hunter test stand (<= 400 lines)."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import html
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.fake_telegram_server import FakeUser, TelegramMessage
from tools.stand import StandContext, create_stand


@dataclass
class CrawlResult:
    buttons_visited: int = 0
    messages_checked: int = 0
    errors: list[str] = field(default_factory=list)
    states_seen: set[str] = field(default_factory=set)


class ButtonCrawler:
    """Traverse all inline buttons and interactive states on the simulated stand."""

    def __init__(self, stand: StandContext, max_depth: int = 6) -> None:
        self.stand = stand
        self.max_depth = max_depth
        self.result = CrawlResult()
        self.visited_callbacks: set[tuple[int, str]] = set()

    def _verify_message(self, user: FakeUser, msg: TelegramMessage) -> None:
        self.result.messages_checked += 1
        if str(msg.chat_id) != str(user.chat_id):
            self.result.errors.append(
                f"Isolation violation: msg {msg.message_id} meant for {user.chat_id} had chat_id {msg.chat_id}"
            )
        if len(msg.text) > 4096:
            self.result.errors.append(f"Message {msg.message_id} exceeds 4096 chars: {len(msg.text)}")
        # Check technical leaks
        lower = msg.text.lower()
        for forbidden in ("traceback", "operationalerror", "syntaxerror", "exception in", "no active slot has capacity"):
            if forbidden in lower:
                self.result.errors.append(f"Technical error leak in message {msg.message_id}: {forbidden}")

    async def crawl_user(self, user: FakeUser, initial_commands: list[str]) -> None:
        queue: deque[tuple[str, TelegramMessage, int]] = deque()

        # Seed with initial commands
        for cmd in initial_commands:
            pre_count = len(user.messages)
            await user.send_command(cmd)
            try:
                msg = await user.wait_for_message(count_since_last=1, timeout=4.0)
                self._verify_message(user, msg)
                for _btn_text, cb_data in msg.find_buttons():
                    if (user.user_id, cb_data) not in self.visited_callbacks:
                        queue.append((cb_data, msg, 1))
            except TimeoutError:
                self.result.errors.append(f"Timeout on user {user.chat_id} command /{cmd}")

        # Process button queue (BFS)
        while queue:
            cb_data, parent_msg, depth = queue.popleft()
            if (user.user_id, cb_data) in self.visited_callbacks or depth > self.max_depth:
                continue
            self.visited_callbacks.add((user.user_id, cb_data))
            self.result.buttons_visited += 1

            pre_msg_count = len(user.messages)
            last_text = user.last_message.text if user.last_message else ""
            await user.click(cb_data, message=parent_msg)

            # Wait for either a new message or an edited message
            start_t = time.monotonic()
            updated = False
            while time.monotonic() - start_t < 2.5:
                if len(user.messages) > pre_msg_count:
                    updated = True
                    break
                cur = user.last_message
                if cur and cur.text != last_text:
                    updated = True
                    break
                await asyncio.sleep(0.05)

            new_msg = user.last_message
            if new_msg:
                self._verify_message(user, new_msg)
                for _btn_text, next_cb in new_msg.find_buttons():
                    if (user.user_id, next_cb) not in self.visited_callbacks:
                        queue.append((next_cb, new_msg, depth + 1))


async def run_full_crawler() -> CrawlResult:
    stand = await create_stand(owner_chat_id=111111, stranger_chat_id=999999)
    try:
        crawler = ButtonCrawler(stand, max_depth=6)
        # 1. Crawl Stranger
        await crawler.crawl_user(stand.stranger, ["start", "help", "faq", "my"])
        # 2. Crawl Owner
        await crawler.crawl_user(stand.owner, ["start", "help", "faq", "my", "status", "admin"])
        return crawler.result
    finally:
        await stand.stop()


def main() -> None:
    res = asyncio.run(run_full_crawler())
    print(f"Crawl completed! Buttons visited: {res.buttons_visited}, Messages checked: {res.messages_checked}")
    print(f"Errors found ({len(res.errors)}): {res.errors}")
    if res.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
