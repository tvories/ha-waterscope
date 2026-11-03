"""Config flow for Metron WaterScope."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

# Home Assistant validates with Probatio since 2026.9 and types its flow APIs
# against it; older releases only ship voluptuous, which Probatio replaces
# one-for-one.
try:
    import probatio as vol
except ImportError:  # pragma: no cover - Home Assistant before 2026.9
    import voluptuous as vol  # type: ignore[no-redef]
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import Meter, WaterscopeAuthError, WaterscopeClient, WaterscopeError
from .const import CONF_ACCOUNT_ID, CONF_METER_NAMES, DEFAULT_METER_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)
STEP_PASSWORD_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})
STEP_NAME_SCHEMA = vol.Schema({vol.Required(CONF_NAME): str})


class WaterscopeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup, reauthentication and reconfiguration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._credentials: dict[str, Any] = {}
        self._meters: list[Meter] = []
        self._names: dict[str, str] = {}

    async def _async_validate(
        self, email: str, password: str, errors: dict[str, str]
    ) -> tuple[str, list[Meter]] | None:
        """Check the credentials; return the account id and its meters."""
        client = WaterscopeClient(
            async_create_clientsession(self.hass),
            email,
            password,
            dt_util.get_default_time_zone(),
        )
        try:
            await client.async_login()
            meters = await client.async_get_meters()
        except WaterscopeAuthError:
            errors["base"] = "invalid_auth"
            return None
        except WaterscopeError as err:
            _LOGGER.debug("WaterScope connection error: %s", err)
            errors["base"] = "cannot_connect"
            return None
        except Exception:
            _LOGGER.exception("Unexpected error validating WaterScope credentials")
            errors["base"] = "unknown"
            return None

        if not meters:
            errors["base"] = "no_meters"
            return None
        return client.account_id or meters[0].account_id, meters

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and create the account entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            result = await self._async_validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD], errors
            )
            if result is not None:
                account_id, self._meters = result
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                self._credentials = {**user_input, CONF_ACCOUNT_ID: account_id}
                return await self.async_step_name()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name each meter in turn; the name also prefixes its entity IDs."""
        errors: dict[str, str] = {}
        meter = self._meters[len(self._names)]

        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                self._names[meter.meter_id] = name
                if len(self._names) < len(self._meters):
                    return await self.async_step_name()
                return self.async_create_entry(
                    title=self._credentials[CONF_EMAIL],
                    data={**self._credentials, CONF_METER_NAMES: self._names},
                )

        return self.async_show_form(
            step_id="name",
            data_schema=self.add_suggested_values_to_schema(
                STEP_NAME_SCHEMA, {CONF_NAME: self._suggest_name(meter)}
            ),
            description_placeholders={
                "meter_id": meter.meter_id,
                "model": meter.model or "WaterScope",
                "location": f" at {meter.address}" if meter.address else "",
            },
            errors=errors,
        )

    def _suggest_name(self, meter: Meter) -> str:
        """Suggest a name, numbered if an earlier meter already took it.

        With several meters a generic name says nothing about which property is
        which, so the service address is suggested instead. With one meter the
        generic name wins: the name becomes the entity ID prefix, and
        ``sensor.water_meter_…`` reads better than a street address.
        """
        base = meter.default_name
        if base == DEFAULT_METER_NAME and len(self._meters) > 1 and meter.address:
            base = meter.address
        taken = set(self._names.values())
        if base not in taken:
            return base
        number = 2
        while f"{base} {number}" in taken:
            number += 1
        return f"{base} {number}"

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the password stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a fresh password for the existing account."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            result = await self._async_validate(
                entry.data[CONF_EMAIL], user_input[CONF_PASSWORD], errors
            )
            if result is not None:
                await self.async_set_unique_id(result[0])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_PASSWORD_SCHEMA,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user point the entry at different credentials."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            result = await self._async_validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD], errors
            )
            if result is not None:
                await self.async_set_unique_id(result[0])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates=dict(user_input)
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_EMAIL: entry.data[CONF_EMAIL]}
            ),
            errors=errors,
        )
