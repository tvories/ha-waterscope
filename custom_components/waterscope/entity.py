"""Shared entity base for WaterScope."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import MeterData, WaterscopeCoordinator


class WaterscopeEntity(CoordinatorEntity[WaterscopeCoordinator]):
    """Base entity for a single meter on the account."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self, coordinator: WaterscopeCoordinator, meter_id: str, key: str
    ) -> None:
        """Initialise the entity for one meter."""
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._attr_unique_id = f"{meter_id}_{key}"

        meter = coordinator.data[meter_id].meter
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_id)},
            manufacturer="Metron",
            model=meter.model or "WaterScope",
            name=meter.nickname,
            serial_number=meter_id,
        )

    @property
    def meter_data(self) -> MeterData:
        """Return this meter's slice of the coordinator snapshot."""
        return self.coordinator.data[self._meter_id]

    @property
    def available(self) -> bool:
        """Return True if the meter was present in the last successful poll."""
        return super().available and self._meter_id in self.coordinator.data
