"""Polling and long-term statistics for WaterScope."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import (
    BudgetCycle,
    Meter,
    MeterStatus,
    Reading,
    WaterscopeAuthError,
    WaterscopeClient,
    WaterscopeError,
)
from .const import BACKFILL_DAYS, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type WaterscopeConfigEntry = ConfigEntry[WaterscopeCoordinator]


@dataclass(slots=True)
class MeterData:
    """Snapshot for one meter, handed to the entities."""

    meter: Meter
    today_gallons: float = 0.0
    yesterday_gallons: float = 0.0
    flow_rate: float = 0.0
    last_reading: datetime | None = None
    # Secondary routes; None when the service did not return usable data.
    status: MeterStatus | None = None
    budget: BudgetCycle | None = None


@dataclass(slots=True)
class _StatsCursor:
    """Where a meter's imported statistics currently end."""

    # Local date from which readings must be re-fetched to continue the series.
    start_date: date
    # Cumulative sum at ``last_bucket``; the base the next rows accumulate onto.
    running_sum: float = 0.0
    # UTC timestamp of the newest already-recorded hour, or None if there is
    # no history at all. Buckets at or before it must not be rewritten.
    last_bucket_ts: float | None = None


@dataclass(slots=True)
class _MeterFetch:
    """Readings fetched for one meter, plus the cursor they were fetched for."""

    readings: list[Reading] = field(default_factory=list)
    cursor: _StatsCursor | None = None


class WaterscopeCoordinator(DataUpdateCoordinator[dict[str, MeterData]]):
    """Polls every meter on one account and keeps its statistics topped up."""

    config_entry: WaterscopeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WaterscopeConfigEntry,
        client: WaterscopeClient,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        # Statistics are imported by a background task on first setup, because a
        # 90-day backfill is several megabytes and must not delay startup. Until
        # it completes, polls only refresh the sensors.
        self._statistics_ready = asyncio.Event()

    def statistic_id(self, meter_id: str) -> str:
        """Return the external statistic id for a meter."""
        return f"{DOMAIN}:water_{meter_id}"

    # ── polling ─────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, MeterData]:
        """Fetch every meter, refresh sensor values, and extend statistics."""
        try:
            meters = await self.client.async_get_meters()
        except WaterscopeAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except WaterscopeError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)
        data: dict[str, MeterData] = {}

        for meter in meters:
            fetch = await self._async_fetch_meter(meter, yesterday, today)
            summary = _summarise(meter, fetch.readings, today, yesterday)
            summary.status, summary.budget = await self._async_fetch_extras(meter)
            data[meter.meter_id] = summary
            if fetch.cursor is not None:
                await self._async_write_statistics(
                    meter, fetch.readings, fetch.cursor
                )

        self._async_remove_stale_devices(meters)
        return data

    async def _async_fetch_meter(
        self, meter: Meter, yesterday: date, today: date
    ) -> _MeterFetch:
        """Fetch a window covering both the sensors and any statistics gap.

        One request serves both: the window starts at whichever is earlier, the
        sensor window or the point the statistics series left off.
        """
        cursor: _StatsCursor | None = None
        start = yesterday

        if self._statistics_ready.is_set():
            cursor = await self._async_stats_cursor(meter.meter_id)
            start = min(start, cursor.start_date)

        try:
            readings = await self.client.async_get_consumption(
                meter.meter_id, start, today
            )
        except WaterscopeAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except WaterscopeError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

        return _MeterFetch(readings=readings, cursor=cursor)

    async def _async_fetch_extras(
        self, meter: Meter
    ) -> tuple[MeterStatus | None, BudgetCycle | None]:
        """Fetch leak state and budget usage.

        These are secondary: the consumption data and its statistics are the
        point of the integration, so a failure here degrades the affected
        entities to unavailable rather than failing the whole update.
        """
        status: MeterStatus | None = None
        budget: BudgetCycle | None = None

        try:
            status = await self.client.async_get_meter_status(meter.meter_id)
        except WaterscopeError as err:
            _LOGGER.debug("Meter status unavailable for %s: %s", meter.meter_id, err)

        try:
            budget = await self.client.async_get_budget_cycle(meter.meter_id)
        except WaterscopeError as err:
            _LOGGER.debug("Budget cycle unavailable for %s: %s", meter.meter_id, err)

        return status, budget

    # ── long-term statistics ────────────────────────────────────────────────

    async def async_import_statistics(self) -> None:
        """Import history once at setup, then let polling keep it current.

        Runs as a background task: on a fresh install this pulls
        ``BACKFILL_DAYS`` of 1-minute data, which is several megabytes.
        """
        try:
            for meter_id, meter_data in (self.data or {}).items():
                cursor = await self._async_stats_cursor(meter_id)
                readings = await self.client.async_get_consumption(
                    meter_id, cursor.start_date, dt_util.now().date()
                )
                await self._async_write_statistics(
                    meter_data.meter, readings, cursor
                )
        except WaterscopeError as err:
            # Leave the gate closed; the next setup retries the import.
            _LOGGER.warning("WaterScope statistics import failed: %s", err)
            return

        self._statistics_ready.set()

    async def _async_stats_cursor(self, meter_id: str) -> _StatsCursor:
        """Find where this meter's statistics series currently ends."""
        statistic_id = self.statistic_id(meter_id)
        recorder = get_instance(self.hass)

        last_stat = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )
        if not last_stat:
            return _StatsCursor(
                start_date=dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
            )

        last_bucket_ts = float(last_stat[statistic_id][0]["start"])
        last_bucket = dt_util.utc_from_timestamp(last_bucket_ts)

        # The running total must continue from the sum recorded at that bucket,
        # not from zero — sums are cumulative across the whole series, so
        # restarting them would leave a cliff in the energy dashboard.
        stats = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            last_bucket,
            last_bucket + timedelta(seconds=1),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        records = stats.get(statistic_id) or []
        recorded_sum = records[0].get("sum") if records else None
        running_sum = float(recorded_sum) if recorded_sum is not None else 0.0

        return _StatsCursor(
            start_date=dt_util.as_local(last_bucket).date(),
            running_sum=running_sum,
            last_bucket_ts=last_bucket_ts,
        )

    async def _async_write_statistics(
        self, meter: Meter, readings: list[Reading], cursor: _StatsCursor
    ) -> None:
        """Roll readings into hourly sums and append them to the series."""
        hourly: dict[datetime, float] = {}
        for reading in readings:
            if reading.missing:
                continue
            bucket = reading.timestamp.replace(minute=0, second=0, microsecond=0)
            hourly[bucket] = hourly.get(bucket, 0.0) + reading.gallons

        running = cursor.running_sum
        rows: list[StatisticData] = []
        for bucket in sorted(hourly):
            # Hours already in the series keep their recorded sum; rewriting
            # them from a fresh running total would corrupt everything after.
            if (
                cursor.last_bucket_ts is not None
                and bucket.timestamp() <= cursor.last_bucket_ts
            ):
                continue
            running += hourly[bucket]
            rows.append(
                StatisticData(start=bucket, state=hourly[bucket], sum=running)
            )

        if not rows:
            return

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{meter.nickname} water",
            source=DOMAIN,
            statistic_id=self.statistic_id(meter.meter_id),
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.GALLONS,
        )
        async_add_external_statistics(self.hass, metadata, rows)
        _LOGGER.debug(
            "Wrote %s hourly statistics rows for meter %s", len(rows), meter.meter_id
        )

    # ── device lifecycle ────────────────────────────────────────────────────

    def _async_remove_stale_devices(self, meters: list[Meter]) -> None:
        """Detach devices for meters that are no longer on the account."""
        current = {meter.meter_id for meter in meters}
        registry = dr.async_get(self.hass)
        entry_id = self.config_entry.entry_id

        for device in dr.async_entries_for_config_entry(registry, entry_id):
            meter_ids = {
                identifier[1]
                for identifier in device.identifiers
                if identifier[0] == DOMAIN
            }
            if meter_ids and not meter_ids & current:
                _LOGGER.debug("Removing stale WaterScope device %s", device.name)
                registry.async_update_device(
                    device.id, remove_config_entry_id=entry_id
                )


def _summarise(
    meter: Meter, readings: list[Reading], today: date, yesterday: date
) -> MeterData:
    """Reduce interval readings to the values the sensors expose."""
    today_readings = [r for r in readings if r.timestamp.date() == today]

    # Readings arrive about a day late, so the current day is usually entirely
    # flagged missing. Taking the newest reading that actually carries data —
    # whatever day it falls on — keeps these two sensors meaningful instead of
    # pinning them at zero until the batch lands.
    with_data = [r for r in readings if not r.missing and r.gallons > 0]
    latest = with_data[-1] if with_data else None

    return MeterData(
        meter=meter,
        today_gallons=round(
            sum(r.gallons for r in today_readings if not r.missing), 2
        ),
        yesterday_gallons=round(
            sum(
                r.gallons
                for r in readings
                if r.timestamp.date() == yesterday and not r.missing
            ),
            2,
        ),
        flow_rate=latest.flow_rate if latest else 0.0,
        last_reading=latest.timestamp if latest else None,
    )
