"""Fixtures for the WaterScope tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.waterscope.api import (
    BudgetCycle,
    Meter,
    MeterStatus,
    Reading,
)
from custom_components.waterscope.const import CONF_ACCOUNT_ID, DOMAIN

ACCOUNT_ID = "1234"
METER_ID = "9000001"
EMAIL = "user@example.com"
PASSWORD = "hunter2"

METER = Meter(
    meter_id=METER_ID,
    nickname="Spectrum PD",
    model="Prism",
    account_id=ACCOUNT_ID,
    is_residential=True,
)
SECOND_METER = Meter(
    meter_id="9000002",
    nickname="Back yard",
    model="Prism",
    account_id=ACCOUNT_ID,
    is_residential=True,
)

# Each generated reading is this many gallons, twice an hour, so an hourly
# statistics bucket is always 2.0 gallons and totals are easy to assert on.
GALLONS_PER_READING = 1.0
READINGS_PER_HOUR = 2

STATUS = MeterStatus(
    leak=False,
    leak_rate=0.0,
    low_temperature=False,
    temperature=77.0,
    budget_status="Tier 3 Rates",
    budget_percentage=466.1,
)
BUDGET = BudgetCycle(
    total=10022.34,
    indoor=4839.68,
    irrigation=4820.56,
    leak=362.1,
    budget=2000.0,
    budget_percentage=501.12,
    daily_target=64.52,
    daily_average=357.94,
    day_of_cycle=28,
    status="Tier 3 Rates",
    cycle_start=datetime(2026, 7, 15, tzinfo=UTC),
    cycle_end=datetime(2026, 8, 14, tzinfo=UTC),
)


@pytest.fixture(autouse=True)
def auto_recorder(recorder_mock: object) -> None:
    """Provide the recorder, which the integration declares as a dependency.

    Must resolve before anything pulls in ``hass``: the recorder fixtures
    assert that no Home Assistant instance has been created yet.
    """


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    auto_recorder: None,
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading custom integrations in every test."""
    yield


@pytest.fixture(autouse=True)
def use_us_customary_units(
    auto_enable_custom_integrations: None, hass: HomeAssistant
) -> None:
    """Report in gallons, as the service does, instead of converting to litres."""
    hass.config.units = US_CUSTOMARY_SYSTEM


def build_readings(start: date, end: date) -> list[Reading]:
    """Build evenly spaced readings across an inclusive local date range."""
    tz = dt_util.get_default_time_zone()
    readings: list[Reading] = []
    day = start
    while day <= end:
        for hour in range(24):
            for slot in range(READINGS_PER_HOUR):
                readings.append(
                    Reading(
                        timestamp=datetime(
                            day.year,
                            day.month,
                            day.day,
                            hour,
                            slot * (60 // READINGS_PER_HOUR),
                            tzinfo=tz,
                        ),
                        gallons=GALLONS_PER_READING,
                        flow_rate=0.5,
                    )
                )
        day += timedelta(days=1)
    return readings


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch the API client everywhere it is constructed."""
    with (
        patch(
            "custom_components.waterscope.WaterscopeClient", autospec=True
        ) as client_class,
        patch(
            "custom_components.waterscope.config_flow.WaterscopeClient",
            new=client_class,
        ),
    ):
        client = client_class.return_value
        client.account_id = ACCOUNT_ID
        client.async_login = AsyncMock(return_value=None)
        client.async_get_meters = AsyncMock(return_value=[METER])
        client.async_get_consumption = AsyncMock(
            side_effect=lambda meter_id, start, end: build_readings(start, end)
        )
        client.async_get_meter_status = AsyncMock(return_value=STATUS)
        client.async_get_budget_cycle = AsyncMock(return_value=BUDGET)
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured WaterScope entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=ACCOUNT_ID,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_ACCOUNT_ID: ACCOUNT_ID,
        },
    )
