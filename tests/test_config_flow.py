"""Tests for the WaterScope config flow."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.waterscope.api import (
    Meter,
    WaterscopeAuthError,
    WaterscopeConnectionError,
)
from custom_components.waterscope.const import (
    CONF_ACCOUNT_ID,
    CONF_METER_NAMES,
    DOMAIN,
)

from .conftest import ACCOUNT_ID, EMAIL, METER_ID, PASSWORD


def _suggested_name(result: Any) -> str:
    """Return the name the form pre-fills."""
    for key in result["data_schema"].schema:
        if key == CONF_NAME:
            return str(key.description["suggested_value"])
    raise AssertionError("no name field on the form")


def _placeholder(result: Any, key: str) -> str:
    """Return one description placeholder from a form result."""
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    return str(placeholders[key])


def _unnamed(meter_id: str, address: str = "") -> Meter:
    """A meter whose nickname Metron defaulted to its ID, as on real accounts."""
    return Meter(
        meter_id=meter_id,
        nickname=meter_id,
        model="Prism",
        account_id=ACCOUNT_ID,
        is_residential=True,
        address=address,
    )


USER_INPUT = {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[None]:
    """Stop the flow from setting the entry up; that is tested separately."""
    with patch("custom_components.waterscope.async_setup_entry", return_value=True):
        yield


async def test_user_flow_creates_entry(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A valid login produces one entry for the account."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    # The owner's own nickname from the WaterScope app is offered first.
    assert _suggested_name(result) == "Spectrum PD"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "  Main house  "}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {
        **USER_INPUT,
        CONF_ACCOUNT_ID: ACCOUNT_ID,
        CONF_METER_NAMES: {METER_ID: "Main house"},
    }
    assert result["result"].unique_id == ACCOUNT_ID


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (WaterscopeAuthError("nope"), "invalid_auth"),
        (WaterscopeConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    side_effect: Exception,
    expected: str,
) -> None:
    """Login failures are shown on the form, and retrying still works."""
    mock_client.async_login.side_effect = side_effect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    mock_client.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"


async def test_user_flow_without_meters(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An account with no meters cannot be configured."""
    mock_client.async_get_meters.return_value = []

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_meters"}


async def test_user_flow_duplicate_account_aborts(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """The same account cannot be added twice."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Reauth stores the new password on the existing entry."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_rejects_a_different_account(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Credentials for another account must not take over the entry."""
    config_entry.add_to_hass(hass)
    mock_client.account_id = "9999"

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_shows_errors(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """A bad password during reauth keeps the form open."""
    config_entry.add_to_hass(hass)
    mock_client.async_login.side_effect = WaterscopeAuthError("nope")

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_updates_credentials(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Reconfigure can change both email and password."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "other@example.com", CONF_PASSWORD: "another"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_EMAIL] == "other@example.com"
    assert config_entry.data[CONF_PASSWORD] == "another"


async def test_reconfigure_rejects_a_different_account(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Reconfigure must stay on the same WaterScope account."""
    config_entry.add_to_hass(hass)
    mock_client.account_id = "9999"

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: "other@example.com", CONF_PASSWORD: "another"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_reconfigure_shows_errors(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Connection failures during reconfigure keep the form open."""
    config_entry.add_to_hass(hass)
    mock_client.async_login.side_effect = WaterscopeConnectionError("down")

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unset_nickname_suggests_a_generic_name(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Metron fills an unset nickname with the meter ID; don't offer that.

    A single meter gets the generic name even when an address is known, because
    the name becomes the entity ID prefix.
    """
    mock_client.async_get_meters.return_value = [
        _unnamed("3700001", address="1 Main St")
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert _suggested_name(result) == "Water meter"
    # The address is still shown, so the user knows which meter this is.
    assert _placeholder(result, "location") == " at 1 Main St"


async def test_several_meters_are_each_named(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Each meter gets its own prompt, suggesting its address to tell them apart."""
    mock_client.async_get_meters.return_value = [
        _unnamed("3700001", address="1 Main St"),
        _unnamed("3700002", address="9 Farm Rd"),
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert _placeholder(result, "meter_id") == "3700001"
    assert _suggested_name(result) == "1 Main St"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "House"}
    )
    assert result["step_id"] == "name"
    assert _placeholder(result, "meter_id") == "3700002"
    assert _suggested_name(result) == "9 Farm Rd"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Barn"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_METER_NAMES] == {"3700001": "House", "3700002": "Barn"}


async def test_suggestions_do_not_collide(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Without addresses, repeated generic names are numbered."""
    mock_client.async_get_meters.return_value = [
        _unnamed("3700001"),
        _unnamed("3700002"),
        _unnamed("3700003"),
    ]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert _suggested_name(result) == "Water meter"
    assert _placeholder(result, "location") == ""

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Water meter"}
    )
    assert _suggested_name(result) == "Water meter 2"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Water meter 2"}
    )
    assert _suggested_name(result) == "Water meter 3"


async def test_blank_name_is_rejected(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A whitespace-only name keeps the form open."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    assert result["errors"] == {CONF_NAME: "name_required"}
