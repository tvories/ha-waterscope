"""Tests for setting up and tearing down the WaterScope entry."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.waterscope.api import (
    WaterscopeAuthError,
    WaterscopeConnectionError,
)
from custom_components.waterscope.const import DOMAIN

from .conftest import METER, METER_ID, SECOND_METER


async def test_setup_and_unload(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """The entry loads, creates a device per meter, and unloads cleanly."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    loaded_state = config_entry.state
    assert loaded_state is ConfigEntryState.LOADED

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, METER_ID)})
    assert device is not None
    assert device.manufacturer == "Metron"
    assert device.name == METER.nickname

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    unloaded_state = config_entry.state
    assert unloaded_state is ConfigEntryState.NOT_LOADED


async def test_setup_with_bad_credentials_starts_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Rejected credentials put the entry into the reauth state."""
    mock_client.async_login.side_effect = WaterscopeAuthError("nope")
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_setup_retries_when_unreachable(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A transport failure makes setup retry rather than fail permanently."""
    mock_client.async_login.side_effect = WaterscopeConnectionError("down")
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_new_meter_is_added_dynamically(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A meter that appears later gets its own device and entities."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    assert (
        registry.async_get_device(identifiers={(DOMAIN, SECOND_METER.meter_id)}) is None
    )

    mock_client.async_get_meters.return_value = [METER, SECOND_METER]
    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        registry.async_get_device(identifiers={(DOMAIN, SECOND_METER.meter_id)})
        is not None
    )
    assert hass.states.get("sensor.back_yard_water_used_today") is not None


async def test_removed_meter_device_is_detached(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A meter that disappears from the account loses its device."""
    mock_client.async_get_meters.return_value = [METER, SECOND_METER]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    assert (
        registry.async_get_device(identifiers={(DOMAIN, SECOND_METER.meter_id)})
        is not None
    )

    mock_client.async_get_meters.return_value = [METER]
    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        registry.async_get_device(identifiers={(DOMAIN, SECOND_METER.meter_id)}) is None
    )
    assert registry.async_get_device(identifiers={(DOMAIN, METER_ID)}) is not None


@pytest.mark.parametrize(
    ("side_effect", "expected_state"),
    [
        (WaterscopeAuthError("nope"), ConfigEntryState.LOADED),
        (WaterscopeConnectionError("down"), ConfigEntryState.LOADED),
    ],
)
async def test_poll_failures_do_not_unload_the_entry(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    side_effect: Exception,
    expected_state: ConfigEntryState,
) -> None:
    """A failed poll marks entities unavailable but keeps the entry loaded."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_client.async_get_meters.side_effect = side_effect
    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert config_entry.state is expected_state
    assert coordinator.last_update_success is False


async def test_manual_device_removal_is_allowed_only_for_gone_meters(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A live meter's device cannot be deleted by hand; a dead one can."""
    from custom_components.waterscope import async_remove_config_entry_device

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    live = registry.async_get_device(identifiers={(DOMAIN, METER_ID)})
    assert live is not None
    assert not await async_remove_config_entry_device(hass, config_entry, live)

    orphan = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "meter-that-was-sold")},
    )
    assert await async_remove_config_entry_device(hass, config_entry, orphan)


@pytest.mark.parametrize(
    ("side_effect", "expects_reauth"),
    [
        (WaterscopeAuthError("nope"), True),
        (WaterscopeConnectionError("down"), False),
    ],
)
async def test_consumption_failures_during_a_poll(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    side_effect: Exception,
    expects_reauth: bool,
) -> None:
    """Failing to fetch readings is handled like any other update failure."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_client.async_get_consumption.side_effect = side_effect
    coordinator = config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    reauth_started = any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )
    assert reauth_started is expects_reauth
