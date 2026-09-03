"""Sensors for Metron WaterScope."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    MeterData,
    WaterscopeConfigEntry,
    WaterscopeCoordinator,
)
from .entity import WaterscopeEntity

# All data comes from a single coordinator poll; entities never fetch on their own.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class WaterscopeSensorDescription(SensorEntityDescription):
    """Describes a WaterScope sensor."""

    value_fn: Callable[[MeterData], float | datetime | str | None]
    # Cycle figures are meaningless without the window they cover, which is set
    # by the utility and is not necessarily the calendar month the WaterScope
    # web dashboard draws.
    attrs_fn: Callable[[MeterData], dict[str, Any]] | None = None


def _cycle_window(data: MeterData) -> dict[str, Any]:
    """Expose the billing cycle this figure covers."""
    if data.budget is None:
        return {}
    return {
        "cycle_start": data.budget.cycle_start,
        "cycle_end": data.budget.cycle_end,
        "day_of_cycle": data.budget.day_of_cycle,
    }


SENSORS: tuple[WaterscopeSensorDescription, ...] = (
    WaterscopeSensorDescription(
        key="today",
        translation_key="today",
        device_class=SensorDeviceClass.WATER,
        # Resets at local midnight — exactly what TOTAL_INCREASING expects;
        # long-term history comes from external statistics instead.
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=1,
        value_fn=lambda data: data.today_gallons,
    ),
    WaterscopeSensorDescription(
        key="yesterday",
        translation_key="yesterday",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=1,
        value_fn=lambda data: data.yesterday_gallons,
    ),
    WaterscopeSensorDescription(
        key="flow_rate",
        translation_key="flow_rate",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.GALLONS_PER_MINUTE,
        suggested_display_precision=2,
        value_fn=lambda data: data.flow_rate,
    ),
    WaterscopeSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Readings arrive about a day late; this is how far the data reaches.
        value_fn=lambda data: data.last_reading,
    ),
    # ── leak / temperature, from the meter status route ──────────────────────
    WaterscopeSensorDescription(
        key="leak_rate",
        translation_key="leak_rate",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.GALLONS_PER_MINUTE,
        suggested_display_precision=2,
        # The service reports gallons per hour.
        value_fn=lambda data: (
            data.status.leak_rate / 60 if data.status else None
        ),
    ),
    WaterscopeSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        # The service reports one value per day and it is that day's *low*, not
        # a live reading. Combined with the ~1-day data lag this changes at most
        # once a day, so it is a daily statistic rather than a measurement.
        state_class=None,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda data: data.status.temperature if data.status else None,
    ),
    # ── budget cycle ─────────────────────────────────────────────────────────
    WaterscopeSensorDescription(
        key="cycle_usage",
        translation_key="cycle_usage",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.total if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="indoor_usage",
        translation_key="indoor_usage",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.indoor if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="irrigation_usage",
        translation_key="irrigation_usage",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.irrigation if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="leak_usage",
        translation_key="leak_usage",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.leak if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="cycle_budget",
        translation_key="cycle_budget",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.budget if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="budget_used",
        translation_key="budget_used",
        attrs_fn=_cycle_window,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda data: (
            data.budget.budget_percentage if data.budget else None
        ),
    ),
    WaterscopeSensorDescription(
        key="daily_average",
        translation_key="daily_average",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.daily_average if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="daily_target",
        translation_key="daily_target",
        attrs_fn=_cycle_window,
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda data: data.budget.daily_target if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="cycle_start",
        translation_key="cycle_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # The utility moves this; it is not necessarily the 1st of the month,
        # and it can roll over a day or two after the cycle actually changes.
        value_fn=lambda data: data.budget.cycle_start if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="cycle_end",
        translation_key="cycle_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.budget.cycle_end if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="cycle_day",
        translation_key="cycle_day",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.budget.day_of_cycle if data.budget else None,
    ),
    WaterscopeSensorDescription(
        key="budget_status",
        translation_key="budget_status",
        attrs_fn=_cycle_window,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.budget.status if data.budget else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaterscopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, adding meters as they appear on the account."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_meters() -> None:
        new = set(coordinator.data) - known
        if not new:
            return
        known.update(new)
        async_add_entities(
            WaterscopeSensor(coordinator, meter_id, description)
            for meter_id in new
            for description in SENSORS
        )

    _async_add_new_meters()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_meters))


class WaterscopeSensor(WaterscopeEntity, SensorEntity):
    """A sensor derived from the coordinator snapshot."""

    entity_description: WaterscopeSensorDescription

    def __init__(
        self,
        coordinator: WaterscopeCoordinator,
        meter_id: str,
        description: WaterscopeSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, meter_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | datetime | str | None:
        """Return the current value."""
        return self.entity_description.value_fn(self.meter_data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the billing window a cycle figure covers, if applicable."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.meter_data)
