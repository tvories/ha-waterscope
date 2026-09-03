"""Tests for the WaterScope sensors."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.waterscope.api import Reading, WaterscopeConnectionError

from .conftest import GALLONS_PER_READING, METER_ID, READINGS_PER_HOUR

# build_readings covers whole days, so a full day is 24 hours of readings.
GALLONS_PER_DAY = GALLONS_PER_READING * READINGS_PER_HOUR * 24


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_sensor_values(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Daily totals and flow rate come through with the right units."""
    await _setup(hass, config_entry)

    today = hass.states.get("sensor.spectrum_pd_water_used_today")
    assert today is not None
    assert float(today.state) == GALLONS_PER_DAY
    assert today.attributes["unit_of_measurement"] == "gal"
    assert today.attributes["state_class"] == "total_increasing"
    assert today.attributes["device_class"] == "water"

    yesterday = hass.states.get("sensor.spectrum_pd_water_used_yesterday")
    assert yesterday is not None
    assert float(yesterday.state) == GALLONS_PER_DAY

    flow = hass.states.get("sensor.spectrum_pd_flow_rate")
    assert flow is not None
    assert float(flow.state) == 0.5
    assert flow.attributes["device_class"] == "volume_flow_rate"


async def test_last_reading_reports_how_far_data_reaches(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Readings lag ~a day, so this diagnostic sensor is enabled and useful."""
    await _setup(hass, config_entry)

    registry = er.async_get(hass)
    entry = registry.async_get("sensor.spectrum_pd_last_reading")

    assert entry is not None
    assert entry.disabled_by is None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
    assert hass.states.get("sensor.spectrum_pd_last_reading") is not None


async def test_unique_ids_are_per_meter_and_key(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Each sensor has a stable unique id scoped to its meter."""
    await _setup(hass, config_entry)

    registry = er.async_get(hass)
    entry = registry.async_get("sensor.spectrum_pd_water_used_today")

    assert entry is not None
    assert entry.unique_id == f"{METER_ID}_today"


async def test_sensors_go_unavailable_after_a_failed_poll(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A failed update marks the entities unavailable rather than stale."""
    await _setup(hass, config_entry)
    before = hass.states.get("sensor.spectrum_pd_water_used_today")
    assert before is not None
    assert before.state != STATE_UNAVAILABLE

    mock_client.async_get_meters.side_effect = WaterscopeConnectionError("down")
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    after = hass.states.get("sensor.spectrum_pd_water_used_today")
    assert after is not None
    assert after.state == STATE_UNAVAILABLE


async def test_budget_sensors(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Budget-cycle figures are exposed with the right units."""
    await _setup(hass, config_entry)

    leak_usage = hass.states.get("sensor.spectrum_pd_leak_usage")
    assert leak_usage is not None
    assert float(leak_usage.state) == 362.1
    assert leak_usage.attributes["device_class"] == "water"

    used = hass.states.get("sensor.spectrum_pd_budget_used")
    assert used is not None
    assert float(used.state) == 501.12
    assert used.attributes["unit_of_measurement"] == "%"

    status = hass.states.get("sensor.spectrum_pd_budget_status")
    assert status is not None
    assert status.state == "Tier 3 Rates"

    irrigation = hass.states.get("sensor.spectrum_pd_irrigation_usage")
    assert irrigation is not None
    assert float(irrigation.state) == 4820.56


async def test_leak_rate_is_converted_to_per_minute(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """The service reports gallons per hour; the sensor is per minute."""
    from dataclasses import replace

    from .conftest import STATUS

    mock_client.async_get_meter_status.return_value = replace(STATUS, leak_rate=6.0)
    await _setup(hass, config_entry)

    rate = hass.states.get("sensor.spectrum_pd_leak_rate")
    assert rate is not None
    assert float(rate.state) == 0.1


async def test_flow_rate_survives_a_day_with_no_data(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Readings lag ~a day, so today being empty must not zero these sensors."""
    from .conftest import build_readings

    def _today_is_missing(meter_id: str, start: date, end: date) -> list[Reading]:
        readings = build_readings(start, end)
        today = dt_util.now().date()
        for reading in readings:
            if reading.timestamp.date() == today:
                reading.missing = True
                reading.gallons = 0.0
                reading.flow_rate = 0.0
        return readings

    mock_client.async_get_consumption.side_effect = _today_is_missing
    await _setup(hass, config_entry)

    # Today is genuinely zero, and says so.
    today_state = hass.states.get("sensor.spectrum_pd_water_used_today")
    assert today_state is not None
    assert float(today_state.state) == 0.0

    # But flow rate and last reading fall back to the newest day that has data.
    flow = hass.states.get("sensor.spectrum_pd_flow_rate")
    assert flow is not None
    assert float(flow.state) > 0

    last = hass.states.get("sensor.spectrum_pd_last_reading")
    assert last is not None
    assert last.state not in (STATE_UNAVAILABLE, "unknown")


async def test_cycle_sensors_expose_their_window(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Cycle figures cover the utility's window, not the calendar month.

    The WaterScope web dashboard shows month-to-date, so without the window
    attached these sensors look wrong when compared against it.
    """
    await _setup(hass, config_entry)

    usage = hass.states.get("sensor.spectrum_pd_cycle_usage")
    assert usage is not None
    assert usage.attributes["cycle_start"].date().isoformat() == "2026-07-15"
    assert usage.attributes["cycle_end"].date().isoformat() == "2026-08-14"
    assert usage.attributes["day_of_cycle"] == 28

    # The window is also available as first-class entities.
    start = hass.states.get("sensor.spectrum_pd_cycle_start")
    assert start is not None
    assert start.state.startswith("2026-07-15")
    end = hass.states.get("sensor.spectrum_pd_cycle_end")
    assert end is not None
    assert end.state.startswith("2026-08-14")
    day = hass.states.get("sensor.spectrum_pd_day_of_cycle")
    assert day is not None
    assert int(float(day.state)) == 28

    # Sensors not derived from the budget cycle carry no window.
    today = hass.states.get("sensor.spectrum_pd_water_used_today")
    assert today is not None
    assert "cycle_start" not in today.attributes
