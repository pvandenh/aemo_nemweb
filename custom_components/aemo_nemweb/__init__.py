"""AEMO NEMWEB Integration for Home Assistant.

Provides current spot prices and forecasts from AEMO's NEMWEB data archive.

Features:
- Current 5-minute spot prices (P5MIN actual dispatch)
- 5-minute price forecasts (P5MINFCST - up to 1 hour ahead)
- 30-minute price forecasts (Predispatch - up to 48 hours ahead)
- EMHASS-compatible forecast format
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import AEMOCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AEMO from a config entry."""
    _LOGGER.info("Setting up AEMO NEMWEB integration for entry: %s", entry.entry_id)
    
    hass.data.setdefault(DOMAIN, {})

    # Merge data and options for current config
    config = {**entry.data, **entry.options}
    _LOGGER.debug("Config: %s", config)

    try:
        # Create and initialize coordinator
        _LOGGER.info("Creating AEMO coordinator")
        coordinator = AEMOCoordinator(hass, config)

        # Fetch initial data
        _LOGGER.info("Performing initial data fetch")
        await coordinator.async_config_entry_first_refresh()
        
        # Store coordinator
        hass.data[DOMAIN][entry.entry_id] = coordinator
        _LOGGER.info("AEMO coordinator stored successfully")

        # Set up platforms
        _LOGGER.info("Setting up platforms: %s", PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Register update listener for options changes
        entry.async_on_unload(entry.add_update_listener(async_update_options))

        _LOGGER.info("AEMO NEMWEB integration setup completed successfully")
        return True
        
    except Exception as e:
        _LOGGER.error("Failed to set up AEMO NEMWEB integration: %s", e, exc_info=True)
        raise ConfigEntryNotReady(f"Failed to set up AEMO: {e}") from e


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.info("Options updated, reloading AEMO integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading AEMO NEMWEB integration for entry: %s", entry.entry_id)
    
    try:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

        if unload_ok:
            coordinator: AEMOCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
            await coordinator.async_shutdown()
            _LOGGER.info("AEMO NEMWEB integration unloaded successfully")
        else:
            _LOGGER.warning("Failed to unload AEMO platforms")

        return unload_ok
        
    except Exception as e:
        _LOGGER.error("Error unloading AEMO integration: %s", e, exc_info=True)
        return False