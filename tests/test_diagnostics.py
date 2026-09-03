"""Tests for WaterScope diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .conftest import ACCOUNT_ID, EMAIL, METER_ID, PASSWORD


async def test_diagnostics_redact_credentials(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
) -> None:
    """Diagnostics get pasted into public issues, so secrets must be redacted."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    dumped = str(result)
    assert PASSWORD not in dumped
    assert EMAIL not in dumped
    assert ACCOUNT_ID not in dumped
    # The meter id is the device serial number and identifies the property.
    assert METER_ID not in dumped

    # The nickname commonly defaults to the meter id, so it is not emitted.
    assert "nickname" not in result["meters"][0]

    assert result["last_update_success"] is True
    assert result["meters"][0]["model"] == "Prism"
    assert result["meters"][0]["status"]["leak"] is False
    assert result["meters"][0]["budget"]["leak"] == 362.1
