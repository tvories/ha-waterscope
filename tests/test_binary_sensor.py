"""Tests for the WaterScope leak and temperature binary sensors."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.waterscope.api import WaterscopeConnectionError

from .conftest import STATUS

LEAK = "binary_sensor.spectrum_pd_leak"
COLD = "binary_sensor.spectrum_pd_low_temperature"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> str:
    """Return an entity's state, failing loudly if it does not exist."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} was never created"
    return state.state


async def test_states_follow_meter_status(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A quiet meter reports off; a leaking one reports on."""
    await _setup(hass, config_entry)

    assert _state(hass, LEAK) == STATE_OFF
    assert _state(hass, COLD) == STATE_OFF
    leak = hass.states.get(LEAK)
    assert leak is not None
    assert leak.attributes["device_class"] == "moisture"

    mock_client.async_get_meter_status.return_value = replace(
        STATUS, leak=True, low_temperature=True
    )
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, LEAK) == STATE_ON
    assert _state(hass, COLD) == STATE_ON


async def test_unavailable_when_status_route_returns_nothing(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """No status means unavailable, not a false "no leak" reading."""
    await _setup(hass, config_entry)
    assert _state(hass, LEAK) == STATE_OFF

    mock_client.async_get_meter_status.return_value = None
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, LEAK) == STATE_UNAVAILABLE


async def test_status_failure_does_not_fail_the_poll(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """The secondary routes are best-effort; consumption data still updates."""
    await _setup(hass, config_entry)

    mock_client.async_get_meter_status.side_effect = WaterscopeConnectionError("down")
    mock_client.async_get_budget_cycle.side_effect = WaterscopeConnectionError("down")
    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The update as a whole succeeded, so the usage sensors keep working.
    assert coordinator.last_update_success is True
    assert _state(hass, "sensor.spectrum_pd_water_used_yesterday") != STATE_UNAVAILABLE
    # Only the entities that depend on the failed route drop out.
    assert _state(hass, LEAK) == STATE_UNAVAILABLE
