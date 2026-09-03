"""Tests for the long-term statistics import.

These cover the two failure modes that matter for the energy dashboard: history
must actually be backfilled, and the cumulative sum must never restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.waterscope.const import BACKFILL_DAYS, DOMAIN

from .conftest import GALLONS_PER_READING, METER_ID, READINGS_PER_HOUR

STATISTIC_ID = f"{DOMAIN}:water_{METER_ID}"
GALLONS_PER_HOUR = GALLONS_PER_READING * READINGS_PER_HOUR


async def _get_statistics(hass: HomeAssistant) -> list[Any]:
    """Return every recorded hourly statistic for the meter, oldest first."""
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - timedelta(days=BACKFILL_DAYS + 2),
        None,
        {STATISTIC_ID},
        "hour",
        None,
        {"sum", "state"},
    )
    return stats.get(STATISTIC_ID, [])


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up and let the background statistics import finish."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def test_backfill_imports_history(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A fresh install imports BACKFILL_DAYS of history, not just today."""
    await _setup(hass, config_entry)

    records = await _get_statistics(hass)
    assert records

    oldest = dt_util.utc_from_timestamp(records[0]["start"])
    age_days = (dt_util.utcnow() - oldest).days
    # The whole point of the backfill: history must reach far back, rather
    # than starting at the first poll.
    assert age_days >= BACKFILL_DAYS - 2

    # Every full hour of generated data is the same volume.
    assert records[0]["state"] == GALLONS_PER_HOUR


async def test_sums_are_cumulative_and_monotonic(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """The running sum accumulates across the whole series."""
    await _setup(hass, config_entry)

    records = await _get_statistics(hass)
    sums = [record["sum"] for record in records]

    assert sums == sorted(sums)
    # Each bucket adds exactly one hour of consumption to the total.
    assert sums[0] == GALLONS_PER_HOUR
    assert sums[-1] == GALLONS_PER_HOUR * len(records)


async def test_second_poll_does_not_reset_the_sum(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Re-importing an overlapping window must extend the series, not restart it.

    Each poll re-fetches a window that overlaps hours already recorded. If the
    running total were restarted from zero for that window, the energy
    dashboard would show a cliff where the rewritten hours meet the older ones.
    """
    await _setup(hass, config_entry)

    before = await _get_statistics(hass)
    highest_before = before[-1]["sum"]
    assert highest_before > 0

    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    after = await _get_statistics(hass)
    sums = [record["sum"] for record in after]

    # No point in the series may be lower than the one before it.
    assert sums == sorted(sums)
    # And the total must not have fallen back towards zero.
    assert sums[-1] >= highest_before
    assert min(sums) == GALLONS_PER_HOUR


async def test_missing_intervals_are_excluded(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Readings flagged isDataMissing must not be counted as zero usage."""
    from custom_components.waterscope.api import Reading

    from .conftest import build_readings

    def _with_a_missing_hour(meter_id: str, start: Any, end: Any) -> list[Reading]:
        readings = build_readings(start, end)
        # Blank out the first hour of the range.
        for reading in readings:
            if reading.timestamp.hour == 0 and reading.timestamp.date() == start:
                reading.missing = True
        return readings

    mock_client.async_get_consumption.side_effect = _with_a_missing_hour
    await _setup(hass, config_entry)

    records = await _get_statistics(hass)
    starts = {dt_util.utc_from_timestamp(record["start"]) for record in records}

    tz = dt_util.get_default_time_zone()
    backfill_start = dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
    excluded = datetime(
        backfill_start.year, backfill_start.month, backfill_start.day, 0, 0, tzinfo=tz
    )
    assert dt_util.as_utc(excluded) not in starts


async def test_failed_import_leaves_the_series_untouched(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """If history cannot be fetched, nothing is written and setup still succeeds."""
    from custom_components.waterscope.api import WaterscopeConnectionError

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data
    mock_client.async_get_consumption.side_effect = WaterscopeConnectionError("down")

    await coordinator.async_import_statistics()
    await hass.async_block_till_done()

    # The gate stays shut so a later attempt can still import from scratch.
    assert coordinator._statistics_ready.is_set() is False
