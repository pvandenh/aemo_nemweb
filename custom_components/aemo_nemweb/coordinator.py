"""Data update coordinator - DUAL-SPEED POLLING FOR OPTIMAL PERFORMANCE.

Polling strategy:
- DISPATCH files: Every 5 seconds (real-time spike detection)
- P5MIN files: Every 30 seconds (actual prices + forecasts)
- Predispatch: Every ~5 minutes (long-term forecasts)

This balances responsiveness with efficiency.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aemo_client import AEMOClient
from .const import (
    CONF_NEM_REGION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class AEMOCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator with dual-speed polling for optimal real-time monitoring."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Base polling rate: 5 seconds for DISPATCH monitoring
            update_interval=timedelta(seconds=5),
        )

        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._aemo_client: AEMOClient | None = None

        # Configuration
        self.region = config.get(CONF_NEM_REGION, "NSW1")
        
        # Track last known files
        self._last_dispatch_file: str | None = None
        self._last_p5min_file: str | None = None
        self._last_predispatch_file: str | None = None
        
        # Spike detection state
        self._dispatch_available = False
        
        # Update cycle counters for variable polling
        self._update_count = 0
        
        _LOGGER.info(
            "AEMO Coordinator initialized for %s (DUAL-SPEED: DISPATCH=5s, P5MIN=30s)",
            self.region
        )

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        _LOGGER.info("Setting up AEMO client with dual-speed DISPATCH monitoring")
        try:
            self._session = aiohttp.ClientSession()
            self._aemo_client = AEMOClient(self._session)
            _LOGGER.info("AEMO client ready (DISPATCH + P5MIN + Predispatch)")
        except Exception as e:
            _LOGGER.error("Failed to create AEMO client: %s", e, exc_info=True)
            raise

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data with dual-speed polling strategy."""
        if self._session is None:
            await self._async_setup()

        try:
            data: dict[str, Any] = {
                "realtime_price": None,
                "spot_price": None,
                "p5min_forecast": [],
                "predispatch_forecast": [],
                "spike_info": {},
                "last_update": None,
            }

            if not self._aemo_client:
                raise UpdateFailed("AEMO client not initialized")

            self._update_count += 1

            # DISPATCH: Check EVERY cycle (every 5 seconds)
            try:
                dispatch_prices, dispatch_file = await self._aemo_client.get_dispatch_price_with_file()
                
                if dispatch_file and dispatch_file != self._last_dispatch_file:
                    _LOGGER.info("NEW DISPATCH file: %s (was: %s)", 
                               dispatch_file, self._last_dispatch_file or "none")
                    self._last_dispatch_file = dispatch_file
                    self._dispatch_available = True
                    
                    region_data = dispatch_prices.get(self.region, {})
                    if region_data:
                        data["realtime_price"] = region_data
                        data["last_update"] = region_data.get("timestamp")
                        
                        # Calculate spike detection metrics
                        current_price = region_data.get("price_mwh", 0)
                        data["spike_info"] = self._aemo_client.calculate_spike_info(current_price)
                        
                        _LOGGER.info(
                            "Real-time price for %s: $%.4f/kWh (spike: %s, ratio: %.2fx)",
                            self.region,
                            region_data.get("price_dollars", 0),
                            "YES" if data["spike_info"].get("is_spike") else "no",
                            data["spike_info"].get("spike_ratio", 1.0)
                        )
                else:
                    # Use existing data
                    if self.data:
                        data["realtime_price"] = self.data.get("realtime_price")
                        data["spike_info"] = self.data.get("spike_info", {})
                        
            except Exception as e:
                _LOGGER.debug("DISPATCH not available (will use P5MIN): %s", e)
                self._dispatch_available = False

            # P5MIN: Check every 6 cycles (every 30 seconds)
            should_check_p5min = (self._update_count % 6 == 0)
            
            if should_check_p5min:
                try:
                    p5min_prices, p5min_file = await self._aemo_client.get_current_prices_with_file()
                    
                    if p5min_file != self._last_p5min_file:
                        _LOGGER.info("NEW P5MIN file: %s (was: %s)", 
                                   p5min_file, self._last_p5min_file or "none")
                        self._last_p5min_file = p5min_file
                        
                        region_data = p5min_prices.get(self.region, {})
                        if region_data:
                            data["spot_price"] = region_data
                            if not data["last_update"]:
                                data["last_update"] = region_data.get("timestamp")
                            
                            _LOGGER.info(
                                "Spot price (actual) for %s: $%.4f/kWh at %s",
                                self.region,
                                region_data.get("price_dollars", 0),
                                region_data.get("timestamp", "unknown")
                            )
                        
                        # Update 5-min forecast
                        p5min_forecast = await self._aemo_client.get_p5min_forecast(
                            self.region, periods=12
                        )
                        data["p5min_forecast"] = p5min_forecast
                        _LOGGER.info("Updated 5-min forecast: %d periods", len(p5min_forecast))
                    else:
                        # Use existing data
                        if self.data:
                            data["spot_price"] = self.data.get("spot_price")
                            data["p5min_forecast"] = self.data.get("p5min_forecast", [])
                            
                except Exception as e:
                    _LOGGER.error("Error fetching P5MIN: %s", e, exc_info=True)
                    if self.data:
                        data["spot_price"] = self.data.get("spot_price")
                        data["p5min_forecast"] = self.data.get("p5min_forecast", [])
            else:
                # Not time to check P5MIN, use existing data
                if self.data:
                    data["spot_price"] = self.data.get("spot_price")
                    data["p5min_forecast"] = self.data.get("p5min_forecast", [])

            # Predispatch: Check on startup (cycle 1) or every 60 cycles (every 5 minutes)
            should_check_predispatch = (self._update_count == 1 or self._update_count % 60 == 0)
            
            if should_check_predispatch:
                try:
                    predispatch_forecast, pd_file = await self._aemo_client.get_predispatch_forecast_with_file(
                        self.region, periods=96
                    )
                    
                    if pd_file != self._last_predispatch_file:
                        _LOGGER.info("NEW Predispatch file: %s", pd_file)
                        self._last_predispatch_file = pd_file
                        data["predispatch_forecast"] = predispatch_forecast
                        _LOGGER.info("Updated predispatch: %d periods", len(predispatch_forecast))
                    else:
                        if self.data:
                            data["predispatch_forecast"] = self.data.get("predispatch_forecast", [])
                except Exception as e:
                    _LOGGER.error("Error fetching Predispatch: %s", e, exc_info=True)
                    if self.data:
                        data["predispatch_forecast"] = self.data.get("predispatch_forecast", [])
            else:
                if self.data:
                    data["predispatch_forecast"] = self.data.get("predispatch_forecast", [])

            # Log status periodically (every minute)
            if self._update_count % 12 == 0:
                if self._dispatch_available:
                    _LOGGER.debug(
                        "Polling status: DISPATCH (5s) + P5MIN (30s) + Predispatch (5m) | Cycle: %d",
                        self._update_count
                    )
                else:
                    _LOGGER.debug(
                        "Polling status: P5MIN only (30s) + Predispatch (5m) | Cycle: %d",
                        self._update_count
                    )

            return data

        except Exception as err:
            _LOGGER.error("Error in update cycle: %s", err, exc_info=True)
            raise UpdateFailed(f"Error fetching data: {err}") from err

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        _LOGGER.info("Shutting down AEMO coordinator")
        if self._session:
            await self._session.close()
            self._session = None