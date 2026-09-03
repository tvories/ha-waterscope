"""The Metron WaterScope integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import WaterscopeAuthError, WaterscopeClient, WaterscopeError
from .const import DOMAIN
from .coordinator import WaterscopeConfigEntry, WaterscopeCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: WaterscopeConfigEntry) -> bool:
    """Set up WaterScope from a config entry."""
    client = WaterscopeClient(
        async_create_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        dt_util.get_default_time_zone(),
    )

    try:
        await client.async_login()
    except WaterscopeAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except WaterscopeError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = WaterscopeCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Import history in the background — a 90-day backfill is several megabytes
    # and must not hold up startup. Polling only extends statistics once this
    # has established where the series starts.
    entry.async_create_background_task(
        hass,
        coordinator.async_import_statistics(),
        f"{entry.entry_id}-statistics-import",
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: WaterscopeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a device for a meter that is no longer on the account."""
    coordinator: WaterscopeCoordinator = entry.runtime_data
    return not any(
        identifier[0] == DOMAIN and identifier[1] in coordinator.data
        for identifier in device.identifiers
    )
