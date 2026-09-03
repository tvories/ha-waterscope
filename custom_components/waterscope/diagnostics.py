"""Diagnostics for Metron WaterScope."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNT_ID
from .coordinator import WaterscopeConfigEntry

# Downloaded diagnostics get pasted into public issue reports. The meter list
# also carries the service address, account holder name and consumer id, so
# nothing derived from that payload may be emitted verbatim.
TO_REDACT = {CONF_PASSWORD, CONF_EMAIL, CONF_ACCOUNT_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WaterscopeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "meters": [
            {
                # The nickname often defaults to the meter id, which is the
                # device serial number, so it is deliberately not included.
                "model": data.meter.model,
                "is_residential": data.meter.is_residential,
                "today_gallons": data.today_gallons,
                "yesterday_gallons": data.yesterday_gallons,
                "flow_rate": data.flow_rate,
                "last_reading": data.last_reading.isoformat()
                if data.last_reading
                else None,
                "status": asdict(data.status) if data.status else None,
                "budget": (
                    {
                        key: value.isoformat()
                        if isinstance(value, datetime)
                        else value
                        for key, value in asdict(data.budget).items()
                    }
                    if data.budget
                    else None
                ),
            }
            for data in (coordinator.data or {}).values()
        ],
    }
