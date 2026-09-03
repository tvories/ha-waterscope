"""Config flow for Metron WaterScope."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import WaterscopeAuthError, WaterscopeClient, WaterscopeError
from .const import CONF_ACCOUNT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)
STEP_PASSWORD_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class WaterscopeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup, reauthentication and reconfiguration."""

    VERSION = 1

    async def _async_validate(
        self, email: str, password: str, errors: dict[str, str]
    ) -> str | None:
        """Check the credentials and return the account id, or None on failure."""
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
        return client.account_id or meters[0].account_id

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and create the account entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            account_id = await self._async_validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD], errors
            )
            if account_id is not None:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={**user_input, CONF_ACCOUNT_ID: account_id},
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

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
            account_id = await self._async_validate(
                entry.data[CONF_EMAIL], user_input[CONF_PASSWORD], errors
            )
            if account_id is not None:
                await self.async_set_unique_id(account_id)
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
            account_id = await self._async_validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD], errors
            )
            if account_id is not None:
                await self.async_set_unique_id(account_id)
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
