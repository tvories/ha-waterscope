"""Tests for the WaterScope API client, at the HTTP level."""

from __future__ import annotations

import time
from datetime import date

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.waterscope.api import (
    WaterscopeAuthError,
    WaterscopeClient,
    WaterscopeConnectionError,
)
from custom_components.waterscope.const import (
    API_BASE,
    CONSUMPTION_PATH,
    METER_LIST_PATH,
    TOKEN_PATH,
)

from .conftest import ACCOUNT_ID, EMAIL, METER_ID, PASSWORD

TOKEN_URL = API_BASE + TOKEN_PATH
METER_URL = API_BASE + METER_LIST_PATH
CONSUMPTION_URL = API_BASE + CONSUMPTION_PATH

TOKEN_RESPONSE = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "token_type": "bearer",
    "expires_in": 1799,
    "accountId": ACCOUNT_ID,
    "utility": "Example Water District",
}


def _client(hass: HomeAssistant) -> WaterscopeClient:
    return WaterscopeClient(
        async_create_clientsession(hass),
        EMAIL,
        PASSWORD,
        dt_util.get_default_time_zone(),
    )


def _expire_token(client: WaterscopeClient) -> None:
    """Age the cached token out, as the passage of time would."""
    assert client._token is not None  # noqa: SLF001
    client._token.expires_at = time.monotonic() - 1  # noqa: SLF001


async def test_password_grant_stores_account_id(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A successful login records the account id from the token response."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)

    client = _client(hass)
    await client.async_login()

    assert client.account_id == ACCOUNT_ID
    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][2]["grant_type"] == "password"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (
            400,
            {"error": "invalid_user", "error_description": "Username does not exists."},
        ),
        (400, {"error": "invalid_grant", "error_description": "Bad password"}),
        (401, {}),
    ],
)
async def test_rejected_credentials_raise_auth_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    payload: dict[str, str],
) -> None:
    """The token endpoint's rejection shapes all map to an auth error."""
    aioclient_mock.post(TOKEN_URL, status=status, json=payload)

    with pytest.raises(WaterscopeAuthError):
        await _client(hass).async_login()


async def test_non_json_token_response_is_a_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTML error page must not be mistaken for bad credentials."""
    aioclient_mock.post(TOKEN_URL, text="<html>502 Bad Gateway</html>")

    with pytest.raises(WaterscopeConnectionError):
        await _client(hass).async_login()


async def test_meters_are_parsed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The meter list maps onto Meter objects, skipping entries with no id."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        METER_URL,
        json={
            "nonClusteredMetersInfo": [
                {
                    "meterId": METER_ID,
                    "meterNickName": "Spectrum PD",
                    "meterModel": "Prism",
                    "accountId": ACCOUNT_ID,
                    "isResidential": True,
                },
                {"meterNickName": "broken entry with no id"},
            ],
            "responseCode": 0,
        },
    )

    meters = await _client(hass).async_get_meters()

    assert len(meters) == 1
    assert meters[0].meter_id == METER_ID
    assert meters[0].nickname == "Spectrum PD"
    assert meters[0].is_residential is True


async def test_consumption_parsing_clamps_and_flags(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Negative register corrections clamp to zero; missing data is flagged."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        CONSUMPTION_URL,
        json={
            "consumptionFlowrateData": [
                {
                    "date": "2026-08-09T00:00:00",
                    "consumption": 1.5,
                    "flowrate": 1.5,
                    "isDataMissing": False,
                },
                {
                    "date": "2026-08-09T00:01:00",
                    "consumption": -0.00724,
                    "flowrate": 0.0,
                    "isDataMissing": False,
                },
                {
                    "date": "2026-08-09T00:02:00",
                    "consumption": 0.0,
                    "flowrate": 0.0,
                    "isDataMissing": True,
                },
            ],
            "responseCode": 0,
        },
    )

    readings = await _client(hass).async_get_consumption(
        METER_ID, date(2026, 8, 9), date(2026, 8, 9)
    )

    assert len(readings) == 3
    assert readings[0].gallons == 1.5
    # A negative value would break a monotonically increasing total.
    assert readings[1].gallons == 0.0
    assert readings[2].missing is True
    # Naive wall-clock timestamps get the account's timezone attached.
    assert readings[0].timestamp.tzinfo is not None
    assert readings[0].timestamp.hour == 0


async def test_long_ranges_are_chunked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Ranges beyond the chunk size are split to keep 1-minute resolution."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        CONSUMPTION_URL, json={"consumptionFlowrateData": [], "responseCode": 0}
    )

    await _client(hass).async_get_consumption(
        METER_ID, date(2026, 5, 1), date(2026, 7, 30)
    )

    consumption_calls = [
        call for call in aioclient_mock.mock_calls if str(call[1]).endswith(
            CONSUMPTION_PATH
        )
    ]
    # 91 days at a 31-day chunk size.
    assert len(consumption_calls) == 3

    requested = [call[2] for call in consumption_calls]
    assert requested[0]["StartDate"] == "2026-05-01"
    assert requested[-1]["EndDate"] == "2026-07-30"
    # Chunks must not overlap or leave gaps.
    assert requested[1]["StartDate"] == "2026-06-01"


async def test_expired_token_is_refreshed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An expired access token is renewed with the refresh grant."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)

    client = _client(hass)
    await client.async_login()
    _expire_token(client)

    aioclient_mock.clear_requests()
    aioclient_mock.post(
        TOKEN_URL, json={**TOKEN_RESPONSE, "access_token": "access-2"}
    )
    aioclient_mock.post(METER_URL, json={"nonClusteredMetersInfo": []})

    await client.async_get_meters()

    token_calls = [
        call for call in aioclient_mock.mock_calls if str(call[1]).endswith(TOKEN_PATH)
    ]
    assert token_calls
    assert token_calls[0][2]["grant_type"] == "refresh_token"


async def test_refresh_failure_falls_back_to_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A dead refresh token falls back to the password grant."""
    grants: list[str] = []

    async def _token(
        method: str, url: str, data: dict[str, str]
    ) -> AiohttpClientMockResponse:
        grants.append(data["grant_type"])
        if data["grant_type"] == "refresh_token":
            return AiohttpClientMockResponse(
                method, url, status=400, json={"error": "invalid_grant"}
            )
        return AiohttpClientMockResponse(method, url, json=TOKEN_RESPONSE)

    aioclient_mock.post(TOKEN_URL, side_effect=_token)
    aioclient_mock.post(METER_URL, json={"nonClusteredMetersInfo": []})

    client = _client(hass)
    await client.async_login()
    _expire_token(client)
    await client.async_get_meters()

    assert grants == ["password", "refresh_token", "password"]


async def test_data_route_error_code_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-zero responseCode is surfaced rather than silently ignored."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        METER_URL, json={"responseCode": 5, "message": "Meter not found"}
    )

    with pytest.raises(WaterscopeConnectionError, match="Meter not found"):
        await _client(hass).async_get_meters()


async def test_401_mid_flight_renews_the_token_and_retries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token rejected by a data route is renewed once and the call retried."""
    attempts: list[int] = []

    async def _meters(
        method: str, url: str, data: dict[str, str]
    ) -> AiohttpClientMockResponse:
        attempts.append(1)
        if len(attempts) == 1:
            return AiohttpClientMockResponse(method, url, status=401, text="")
        return AiohttpClientMockResponse(
            method, url, json={"nonClusteredMetersInfo": []}
        )

    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(METER_URL, side_effect=_meters)

    client = _client(hass)
    await client.async_login()
    assert await client.async_get_meters() == []

    assert len(attempts) == 2


async def test_repeated_401_gives_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token that is rejected twice raises rather than looping."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(METER_URL, status=401, text="")

    client = _client(hass)
    await client.async_login()

    with pytest.raises(WaterscopeConnectionError):
        await client.async_get_meters()


async def test_transport_failure_on_a_data_route(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A dropped connection surfaces as a connection error."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(METER_URL, exc=ClientError("boom"))

    client = _client(hass)
    await client.async_login()

    with pytest.raises(WaterscopeConnectionError):
        await client.async_get_meters()


async def test_transport_failure_on_the_token_route(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The token endpoint being unreachable is not an auth failure."""
    aioclient_mock.post(TOKEN_URL, exc=ClientError("boom"))

    with pytest.raises(WaterscopeConnectionError):
        await _client(hass).async_login()


async def test_unexpected_token_status(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A server error is a connection problem, not a credential problem."""
    aioclient_mock.post(TOKEN_URL, status=500, json={})

    with pytest.raises(WaterscopeConnectionError):
        await _client(hass).async_login()


async def test_non_object_payload_is_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bare array where an object is expected must not be parsed."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(METER_URL, json=[])

    client = _client(hass)
    await client.async_login()

    with pytest.raises(WaterscopeConnectionError, match="unexpected payload"):
        await client.async_get_meters()


WEEK_URL = API_BASE + "/waterscope/mobile/dashboard/getConsumptionForWeekWithBudget"
CYCLE_URL = (
    API_BASE + "/waterscope/mobile/usageOverview/getBudgetCycleMonthlyDetails"
)


async def test_meter_status_uses_the_newest_day_with_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Readings lag, so the newest day carrying consumption is the live state."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        WEEK_URL,
        json={
            "unit": "G",
            "details": [
                {
                    "consumption": 10.0,
                    "date": "2026-08-09T02:55:39Z",
                    "isConsumptionAvailable": True,
                    "waterBudgetStatusInfo": {"message": "Efficient",
                                              "percentageUse": 10.0},
                    "meterStatusInfo": {
                        "isLeakExists": False,
                        "leakRatePerHour": 0.0,
                        "isLowTemperatureExists": False,
                        "lowTemperature": 70,
                    },
                },
                {
                    "consumption": 300.71,
                    "date": "2026-08-11T02:55:39Z",
                    "isConsumptionAvailable": True,
                    "waterBudgetStatusInfo": {"message": "Tier 3 Rates",
                                              "percentageUse": 466.1},
                    "meterStatusInfo": {
                        "isLeakExists": True,
                        "leakRatePerHour": 6.0,
                        "isLowTemperatureExists": True,
                        "lowTemperature": 77,
                    },
                },
                {
                    # Today: no data yet, must not win despite being newest.
                    "consumption": 0.0,
                    "date": "2026-08-12T02:55:39Z",
                    "isConsumptionAvailable": False,
                    "waterBudgetStatusInfo": {},
                    "meterStatusInfo": {"isLeakExists": False},
                },
            ],
            "responseCode": 0,
        },
    )

    status = await _client(hass).async_get_meter_status(METER_ID)

    assert status is not None
    assert status.leak is True
    assert status.leak_rate == 6.0
    assert status.low_temperature is True
    assert status.temperature == 77
    assert status.budget_status == "Tier 3 Rates"
    assert status.budget_percentage == 466.1


async def test_meter_status_without_details(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An empty week is reported as no status rather than a fabricated one."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(WEEK_URL, json={"details": None, "responseCode": 0})

    assert await _client(hass).async_get_meter_status(METER_ID) is None


async def test_budget_cycle_is_parsed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The budget cycle payload maps onto the dataclass."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        CYCLE_URL,
        json={
            "usageData": {
                "totalConsumption": 10022.34,
                "indoorConsumption": 4839.68,
                "irrigationConsumption": 4820.56,
                "leakConsumption": 362.1,
                "budgetPercentageUse": 501.12,
            },
            "statistics": {
                "dailyTarget": 64.52,
                "cycleBudget": 2000.0,
                "cycleDailyAverage": 357.94,
                "dayOfTheCycle": 28,
                "cycleStartDate": "2026-07-15T00:00:00Z",
                "cycleEndDate": "2026-08-14T00:00:00Z",
            },
            "status": {"message": "Tier 3 Rates"},
            "responseCode": 0,
        },
    )

    budget = await _client(hass).async_get_budget_cycle(METER_ID)

    assert budget is not None
    assert budget.total == 10022.34
    assert budget.leak == 362.1
    assert budget.budget == 2000.0
    assert budget.budget_percentage == 501.12
    assert budget.day_of_cycle == 28
    assert budget.status == "Tier 3 Rates"
    assert budget.cycle_start is not None
    assert budget.cycle_start.year == 2026
    assert budget.cycle_start.tzinfo is not None


async def test_budget_cycle_placeholder_dates_are_dropped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The 0001-01-01 placeholder means "no value", not year one."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        CYCLE_URL,
        json={
            "usageData": {"totalConsumption": 1.0},
            "statistics": {
                "cycleStartDate": "0001-01-01T00:00:00",
                "cycleEndDate": None,
            },
            "status": None,
            "responseCode": 0,
        },
    )

    budget = await _client(hass).async_get_budget_cycle(METER_ID)

    assert budget is not None
    assert budget.cycle_start is None
    assert budget.cycle_end is None
    assert budget.status is None
    # Missing numbers fall back to zero rather than raising.
    assert budget.indoor == 0.0


async def test_budget_cycle_when_service_has_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A meter with no budget configured yields no budget data."""
    aioclient_mock.post(TOKEN_URL, json=TOKEN_RESPONSE)
    aioclient_mock.post(
        CYCLE_URL, json={"usageData": None, "statistics": None, "responseCode": 0}
    )

    assert await _client(hass).async_get_budget_cycle(METER_ID) is None
