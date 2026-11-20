"""Integration test running reproducible monkey / soak test sequences (<= 400 lines)."""

from __future__ import annotations

import pytest

from tools.monkey_test import run_monkey_test


@pytest.mark.asyncio
async def test_monkey_run_seed_42() -> None:
    """Run interleaved monkey actions under seed 42 with invariant checks."""
    metrics = await run_monkey_test(seed=42, steps=25)
    assert metrics.steps_total == 25
    assert not metrics.errors, f"Monkey test had errors: {metrics.errors}"
    assert metrics.actions_count.get("owner_cmd", 0) > 0
    assert metrics.actions_count.get("avito_new_item", 0) > 0


@pytest.mark.asyncio
async def test_monkey_run_seed_99() -> None:
    """Run interleaved monkey actions under seed 99 with invariant checks."""
    metrics = await run_monkey_test(seed=99, steps=25)
    assert metrics.steps_total == 25
    assert not metrics.errors, f"Monkey test had errors: {metrics.errors}"


@pytest.mark.asyncio
async def test_monkey_run_seed_101() -> None:
    """Run interleaved monkey actions under seed 101 with invariant checks."""
    metrics = await run_monkey_test(seed=101, steps=30)
    assert metrics.steps_total == 30
    assert not metrics.errors, f"Monkey test had errors: {metrics.errors}"
    assert metrics.actions_count.get("click_button", 0) > 0


@pytest.mark.asyncio
async def test_monkey_run_seed_202() -> None:
    """Run interleaved monkey actions under seed 202 with invariant checks."""
    metrics = await run_monkey_test(seed=202, steps=30)
    assert metrics.steps_total == 30
    assert not metrics.errors, f"Monkey test had errors: {metrics.errors}"


@pytest.mark.asyncio
async def test_monkey_run_seed_303() -> None:
    """Run interleaved monkey actions under seed 303 with invariant checks."""
    metrics = await run_monkey_test(seed=303, steps=30)
    assert metrics.steps_total == 30
    assert not metrics.errors, f"Monkey test had errors: {metrics.errors}"
