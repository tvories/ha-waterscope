"""Binary sensors for Metron WaterScope."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MeterData, WaterscopeConfigEntry, WaterscopeCoordinator
from .entity import WaterscopeEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WaterscopeBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a WaterScope binary sensor."""

    value_fn: Callable[[MeterData], bool | None]


BINARY_SENSORS: tuple[WaterscopeBinarySensorDescription, ...] = (
    WaterscopeBinarySensorDescription(
        key="leak",
        translation_key="leak",
        device_class=BinarySensorDeviceClass.MOISTURE,
        value_fn=lambda data: data.status.leak if data.status else None,
    ),
    WaterscopeBinarySensorDescription(
        key="low_temperature",
        translation_key="low_temperature",
        device_class=BinarySensorDeviceClass.COLD,
        value_fn=lambda data: (
            data.status.low_temperature if data.status else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterscopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors, adding meters as they appear on the account."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_meters() -> None:
        new = set(coordinator.data) - known
        if not new:
            return
        known.update(new)
        async_add_entities(
            WaterscopeBinarySensor(coordinator, meter_id, description)
            for meter_id in new
            for description in BINARY_SENSORS
        )

    _async_add_new_meters()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_meters))


class WaterscopeBinarySensor(WaterscopeEntity, BinarySensorEntity):
    """A binary sensor derived from the meter's reported status."""

    entity_description: WaterscopeBinarySensorDescription

    def __init__(
        self,
        coordinator: WaterscopeCoordinator,
        meter_id: str,
        description: WaterscopeBinarySensorDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, meter_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the current state."""
        return self.entity_description.value_fn(self.meter_data)

    @property
    def available(self) -> bool:
        """Unavailable when the status route returned nothing usable."""
        return super().available and self.meter_data.status is not None
