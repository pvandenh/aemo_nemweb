"""Config flow for AEMO NEMWEB integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_NEM_REGION,
    DOMAIN,
    NEM_REGIONS,
)

_LOGGER = logging.getLogger(__name__)


class AEMONEMWEBConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AEMO NEMWEB."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - select NEM region."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Check if already configured for this region
            await self.async_set_unique_id(user_input[CONF_NEM_REGION])
            self._abort_if_unique_id_configured()

            title = f"AEMO {user_input[CONF_NEM_REGION]}"
            return self.async_create_entry(title=title, data=user_input)

        region_options = [
            selector.SelectOptionDict(value=code, label=f"{code} - {name}")
            for code, name in NEM_REGIONS.items()
        ]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_NEM_REGION, default="NSW1"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=region_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AEMONEMWEBOptionsFlow:
        """Get the options flow for this handler."""
        return AEMONEMWEBOptionsFlow()


class AEMONEMWEBOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for AEMO NEMWEB."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Currently no options, but can add forecast periods, etc. here
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
        )