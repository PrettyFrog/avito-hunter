"""Estimate polling workload from current assignments and configured policy."""

from typing import Any

from hunter.domain.capacity import CapacityPlan, calculate_capacity_plan


async def estimate_workload(conn: Any, config: Any) -> CapacityPlan:
    """Estimate nominal daily demand; actual pauses, retries and DST can differ."""
    count = 0
    if config.enabled:
        cursor = await conn.execute(
            "SELECT count(*) FROM assignments a JOIN slots s ON s.id = a.slot_id "
            "WHERE a.active = 1 AND lower(s.health_state) NOT IN "
            "('disabled', 'quarantined', 'auth_required', 'challenge')"
        )
        count = int((await cursor.fetchone())[0])
    night_hours = max(0, config.day_start_hour - config.night_start_hour)
    return calculate_capacity_plan(
        count,
        day_hours=24 - night_hours,
        night_hours=night_hours,
        day_interval_sec=config.day_interval_sec,
        night_interval_sec=config.night_interval_sec,
    )
