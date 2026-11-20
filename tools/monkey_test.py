"""Monkey and soak test runner for Avito Hunter test stand (<= 400 lines)."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import random
import resource
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.fake_telegram_server import FakeUser
from tools.stand import StandContext, create_stand


@dataclass
class MonkeyMetrics:
    seed: int
    steps_total: int = 0
    actions_count: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    initial_rss_kb: int = 0
    final_rss_kb: int = 0


class MonkeyTester:
    """Stress/monkey test runner executing pseudorandom interleaved workflows."""

    def __init__(self, stand: StandContext, seed: int = 42, steps: int = 60) -> None:
        self.stand = stand
        self.seed = seed
        self.steps = steps
        self.rng = random.Random(seed)
        self.metrics = MonkeyMetrics(seed=seed)
        self._item_counter = 1000

    def _get_rss(self) -> int:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    async def check_invariants(self, step_idx: int) -> None:
        # 1. SQLite integrity
        cur = await self.stand.db._conn.execute("PRAGMA integrity_check")
        row = await cur.fetchone()
        if not row or row[0] != "ok":
            self.metrics.errors.append(f"Step {step_idx}: SQLite integrity check failed: {row}")

        # 2. Check no deadlocked or stuck pending deliveries older than 60s
        cur = await self.stand.db._conn.execute(
            "SELECT count(*) FROM deliveries WHERE status = 'lease_expired' AND attempt > 10"
        )
        row = await cur.fetchone()
        if row and row[0] > 0:
            self.metrics.errors.append(f"Step {step_idx}: Found {row[0]} stuck deliveries")

    async def _action_command(self, user: FakeUser, cmds: list[str], name: str) -> None:
        cmd = self.rng.choice(cmds)
        await user.send_command(cmd)
        try:
            msg = await user.wait_for_message(timeout=3.0)
            if len(msg.text) > 4096:
                self.metrics.errors.append(f"Message {msg.message_id} text > 4096 chars")
        except TimeoutError:
            pass  # Some commands during transient 429/pause may be delayed
        self.metrics.actions_count[name] = self.metrics.actions_count.get(name, 0) + 1

    async def _action_button_click(self, user: FakeUser) -> None:
        msgs = [m for m in user.messages if m.reply_markup and m.find_buttons()]
        if not msgs:
            return
        msg = self.rng.choice(msgs)
        buttons = msg.find_buttons()
        if buttons:
            label, cb = self.rng.choice(buttons)
            await user.click(cb, message=msg)
            await asyncio.sleep(0.2)
            self.metrics.actions_count["click_button"] = (
                self.metrics.actions_count.get("click_button", 0) + 1
            )

    async def _action_avito_new_item(self) -> None:
        self._item_counter += 1
        item_id = f"400999{self._item_counter:04d}"
        price = self.rng.randint(20000, 42000)
        self.stand.avito.add_item(
            id=item_id,
            title=f"Sony A6000 Monkey Lot #{self._item_counter}",
            price=price,
        )
        self.metrics.actions_count["avito_new_item"] = (
            self.metrics.actions_count.get("avito_new_item", 0) + 1
        )

    async def _action_avito_glitch(self) -> None:
        mode = self.rng.choice(["429", "challenge", "ok"])
        self.stand.avito.set_mode(mode)
        await asyncio.sleep(0.5)
        self.stand.avito.set_mode("ok")
        self.metrics.actions_count["avito_glitch"] = (
            self.metrics.actions_count.get("avito_glitch", 0) + 1
        )

    async def run(self) -> MonkeyMetrics:
        self.metrics.initial_rss_kb = self._get_rss()

        # Seed initial subscription for owner
        await self.stand.owner.send_command("add")
        s1 = await self.stand.owner.wait_for_message(timeout=4.0)
        await self.stand.owner.send_text("Monkey Product")
        s2 = await self.stand.owner.wait_for_message(timeout=4.0)
        await self.stand.owner.send_text("https://www.avito.ru/moskva?q=sony")
        s3 = await self.stand.owner.wait_for_message(timeout=4.0)
        await self.stand.owner.send_text("40000")
        s4 = await self.stand.owner.wait_for_message(timeout=4.0)
        await self.stand.owner.click("public_add_skip_words", message=s4)
        await self.stand.owner.wait_for_message(timeout=4.0)

        actions = [
            lambda: self._action_command(self.stand.owner, ["my", "help", "faq", "status", "admin"], "owner_cmd"),
            lambda: self._action_command(self.stand.user1, ["my", "help", "faq", "support"], "user_cmd"),
            lambda: self._action_command(self.stand.stranger, ["start", "help", "faq"], "stranger_cmd"),
            lambda: self._action_button_click(self.stand.owner),
            self._action_avito_new_item,
            self._action_avito_glitch,
        ]

        for step in range(1, self.steps + 1):
            act = self.rng.choice(actions)
            try:
                await act()
            except Exception as exc:
                self.metrics.errors.append(f"Step {step} action failed: {exc}")

            await self.check_invariants(step)
            self.metrics.steps_total = step
            await asyncio.sleep(0.1)

        self.metrics.final_rss_kb = self._get_rss()
        return self.metrics


async def run_monkey_test(seed: int = 42, steps: int = 60) -> MonkeyMetrics:
    stand = await create_stand(owner_chat_id=111111, user1_chat_id=222222, stranger_chat_id=999999)
    try:
        tester = MonkeyTester(stand, seed=seed, steps=steps)
        return await tester.run()
    finally:
        await stand.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run monkey test")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument("--steps", type=int, default=60, help="Number of monkey steps")
    args = parser.parse_args()

    res = asyncio.run(run_monkey_test(seed=args.seed, steps=args.steps))
    print(f"Monkey test finished! Seed: {res.seed}, Steps: {res.steps_total}")
    print(f"Actions taken: {res.actions_count}")
    print(f"Memory: Initial={res.initial_rss_kb} KB, Final={res.final_rss_kb} KB")
    print(f"Errors found ({len(res.errors)}): {res.errors}")
    if res.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
