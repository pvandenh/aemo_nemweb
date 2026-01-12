"""Data update coordinator - SMART POLLING FOR OPTIMAL PERFORMANCE.

Polling strategy:
- After getting new data: Wait until next 5-minute period
- At period boundary: Poll every 1 second until new data found
- This minimizes API calls while maximizing responsiveness

Example timeline:
12:05:00 - New file downloaded, timestamp shows 12:05
12:05:01 - Wait mode (no polling)
...
12:09:59 - Wait mode (no polling)
12:10:00 - Active polling mode (check every 1 second)
12:10:01 - Check (no new file yet)
12:10:02 - Check (no new file yet)
12:10:03 - Check (NEW FILE!) - Download and return to wait mode
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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
    """Coordinator with smart polling - wait until period boundary, then poll aggressively."""

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
            # Start with 1 second polling (will be dynamically adjusted)
            update_interval=timedelta(seconds=1),
        )

        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._aemo_client: AEMOClient | None = None

        # Configuration
        self.region = config.get(CONF_NEM_REGION, "NSW1")
        
        # Track last known files and their timestamps
        self._last_dispatch_file: str | None = None
        self._last_p5min_file: str | None = None
        self._last_predispatch_file: str | None = None
        
        # Track the current 5-minute period we have data for
        self._current_period_end: datetime | None = None
        
        # Polling mode: 'wait' or 'active'
        self._polling_mode: str = 'active'  # Start in active mode
        
        # Spike detection state
        self._dispatch_available = False
        
        # Update cycle counters
        self._update_count = 0
        self._active_polling_count = 0  # Count checks during active polling
        
        _LOGGER.info(
            "AEMO Coordinator initialized for %s (SMART POLLING: wait until period boundary, then 1s polls)",
            self.region
        )

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        _LOGGER.info("Setting up AEMO client with smart polling")
        try:
            self._session = aiohttp.ClientSession()
            self._aemo_client = AEMOClient(self._session)
            _LOGGER.info("AEMO client ready")
        except Exception as e:
            _LOGGER.error("Failed to create AEMO client: %s", e, exc_info=True)
            raise

    def _parse_aemo_timestamp(self, timestamp_str: str) -> datetime | None:
        """Parse AEMO timestamp string to datetime in local timezone.
        
        IMPORTANT: AEMO data uses AEST (UTC+10) year-round, not local time.
        We need to convert to local time for comparison with datetime.now().
        
        AEMO format: "2025/01/12 13:05:00" (always AEST/UTC+10)
        """
        if not timestamp_str or "/" not in timestamp_str:
            return None
        
        try:
            from datetime import timezone, timedelta
            
            # Parse the timestamp (no timezone yet)
            dt_naive = datetime.strptime(timestamp_str, "%Y/%m/%d %H:%M:%S")
            
            # AEMO always uses AEST (UTC+10), regardless of daylight saving
            aest = timezone(timedelta(hours=10))
            dt_aest = dt_naive.replace(tzinfo=aest)
            
            # Convert to local time (which may be AEDT/UTC+11 during daylight saving)
            dt_local = dt_aest.astimezone()
            
            # Return as naive datetime in local timezone for comparison with datetime.now()
            return dt_local.replace(tzinfo=None)
            
        except (ValueError, TypeError) as e:
            _LOGGER.debug("Failed to parse timestamp '%s': %s", timestamp_str, e)
            return None

    def _get_next_period_boundary(self, current_period: datetime) -> datetime:
        """Get the next 5-minute boundary in local time to start polling.
        
        We want to start polling at every 5-minute mark (XX:00, XX:05, XX:10, etc.)
        regardless of what the file timestamp says.
        
        Example:
        - Current time: 16:50:38
        - Next boundary: 16:55:00 (round up to next 5-minute mark)
        - Start polling at 16:55:00 for new files
        
        Args:
            current_period: Not actually used - we calculate based on current time
            
        Returns:
            The next 5-minute boundary in local time
        """
        now = datetime.now()
        
        # Round up to next 5-minute boundary
        current_minute = now.minute
        current_second = now.second
        
        # Calculate minutes until next 5-minute mark
        next_boundary_minute = ((current_minute // 5) + 1) * 5
        
        if next_boundary_minute >= 60:
            # Roll over to next hour
            next_boundary = now.replace(minute=0, second=0, microsecond=0)
            next_boundary += timedelta(hours=1)
        else:
            next_boundary = now.replace(minute=next_boundary_minute, second=0, microsecond=0)
        
        return next_boundary

    def _should_poll_now(self) -> bool:
        """Determine if we should poll based on current time and period boundary.
        
        Dynamically adjusts update_interval with 3-tier strategy:
        - WAIT mode (>10s until boundary): 45 second checks (minimal overhead)
        - PRE-ACTIVE mode (10s before to 15s after boundary): 5 second checks
        - ACTIVE mode (15s+ after boundary): 1 second checks (rapid polling for new files)
        
        Example timeline for 17:15:00 boundary:
        - 17:14:00-17:14:49: WAIT mode, 45s intervals
        - 17:14:50-17:15:14: PRE-ACTIVE mode, 5s intervals (files never appear this early)
        - 17:15:15+: ACTIVE mode, 1s intervals (files typically appear now)
        
        Returns:
            True if we should poll, False if we should wait
        """
        if self._current_period_end is None:
            # No data yet, always poll
            _LOGGER.debug("_should_poll_now: no period end set, polling")
            return True
        
        now = datetime.now()
        seconds_from_boundary = (now - self._current_period_end).total_seconds()
        
        if seconds_from_boundary < -10:
            # More than 10s before boundary: WAIT mode
            if self._polling_mode != 'wait':
                seconds_until = -seconds_from_boundary
                _LOGGER.info(
                    "Entering WAIT mode until %s (next period boundary in %d seconds)",
                    self._current_period_end.strftime("%H:%M:%S"),
                    int(seconds_until)
                )
                self._polling_mode = 'wait'
                self._active_polling_count = 0
            # Long wait: 45 second intervals
            self.update_interval = timedelta(seconds=45)
            return False
            
        elif seconds_from_boundary < 15:
            # From 10s before to 15s after boundary: PRE-ACTIVE mode
            if self._polling_mode != 'pre_active':
                if seconds_from_boundary < 0:
                    _LOGGER.info(
                        "Entering PRE-ACTIVE mode (5s intervals) - boundary in %d seconds",
                        int(-seconds_from_boundary)
                    )
                else:
                    _LOGGER.info(
                        "Entering PRE-ACTIVE mode (5s intervals) - %d seconds into period",
                        int(seconds_from_boundary)
                    )
                self._polling_mode = 'pre_active'
                self._active_polling_count = 0
            # Pre-active: 5 second intervals (files don't appear yet)
            self.update_interval = timedelta(seconds=5)
            return False
            
        else:
            # 15+ seconds past boundary: ACTIVE mode
            if self._polling_mode != 'active':
                _LOGGER.info(
                    "Entering ACTIVE POLLING mode (1s intervals) - looking for new data"
                )
                self._polling_mode = 'active'
                self._active_polling_count = 0
            # Active polling: 1 second intervals
            self.update_interval = timedelta(seconds=1)
            return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data with smart polling strategy."""
        if self._session is None:
            await self._async_setup()

        self._update_count += 1

        # Check if we should poll now
        if not self._should_poll_now():
            # In wait mode - return existing data WITHOUT any API calls
            if self.data:
                return self.data
            # If no existing data yet, fall through to poll
            # (this only happens on first startup)

        # Active polling mode - check for new data
        self._active_polling_count += 1

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

            # Keep existing data as defaults
            if self.data:
                data["realtime_price"] = self.data.get("realtime_price")
                data["spot_price"] = self.data.get("spot_price")
                data["p5min_forecast"] = self.data.get("p5min_forecast", [])
                data["predispatch_forecast"] = self.data.get("predispatch_forecast", [])
                data["spike_info"] = self.data.get("spike_info", {})

            found_new_data = False

            # Try DISPATCH first (fastest updates, ~2-3 min)
            try:
                dispatch_prices, dispatch_file = await self._aemo_client.get_dispatch_price_with_file()
                
                if dispatch_file and dispatch_file != self._last_dispatch_file:
                    _LOGGER.info(
                        "NEW DISPATCH file found after %d active polls: %s",
                        self._active_polling_count,
                        dispatch_file
                    )
                    self._last_dispatch_file = dispatch_file
                    self._dispatch_available = True
                    found_new_data = True
                    
                    region_data = dispatch_prices.get(self.region, {})
                    if region_data:
                        data["realtime_price"] = region_data
                        timestamp = region_data.get("timestamp")
                        data["last_update"] = timestamp
                        
                        # Update period boundary
                        if timestamp:
                            period_dt = self._parse_aemo_timestamp(timestamp)
                            if period_dt:
                                self._current_period_end = self._get_next_period_boundary(period_dt)
                                _LOGGER.info(
                                    "Period updated from DISPATCH: current=%s, next boundary=%s (now=%s)",
                                    timestamp,
                                    self._current_period_end.strftime("%H:%M:%S"),
                                    datetime.now().strftime("%H:%M:%S")
                                )
                        
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
                elif dispatch_prices and not found_new_data:
                    # File is cached, but we still need to set period boundary on first run
                    if self._current_period_end is None:
                        region_data = dispatch_prices.get(self.region, {})
                        if region_data:
                            timestamp = region_data.get("timestamp")
                            if timestamp:
                                period_dt = self._parse_aemo_timestamp(timestamp)
                                if period_dt:
                                    self._current_period_end = self._get_next_period_boundary(period_dt)
                                    _LOGGER.info(
                                        "Period initialized from cached DISPATCH: current=%s, next boundary=%s",
                                        timestamp,
                                        self._current_period_end.strftime("%H:%M:%S")
                                    )
                        
            except Exception as e:
                _LOGGER.debug("DISPATCH not available: %s", e)
                self._dispatch_available = False

            # Try P5MIN (actual prices, updates every ~5 min)
            try:
                p5min_prices, p5min_file = await self._aemo_client.get_current_prices_with_file()
                
                if p5min_file and p5min_file != self._last_p5min_file:
                    _LOGGER.info(
                        "NEW P5MIN file found after %d active polls: %s",
                        self._active_polling_count,
                        p5min_file
                    )
                    self._last_p5min_file = p5min_file
                    found_new_data = True
                    
                    region_data = p5min_prices.get(self.region, {})
                    if region_data:
                        data["spot_price"] = region_data
                        timestamp = region_data.get("timestamp")
                        if not data["last_update"]:
                            data["last_update"] = timestamp
                        
                        # Update period boundary if we don't have DISPATCH
                        if not self._dispatch_available and timestamp:
                            period_dt = self._parse_aemo_timestamp(timestamp)
                            if period_dt:
                                self._current_period_end = self._get_next_period_boundary(period_dt)
                                _LOGGER.info(
                                    "Period updated from P5MIN: current=%s, next boundary=%s",
                                    timestamp,
                                    self._current_period_end.strftime("%H:%M:%S")
                                )
                        
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
                elif p5min_prices and not found_new_data:
                    # File is cached, but we still need to set period boundary on first run
                    if self._current_period_end is None and not self._dispatch_available:
                        region_data = p5min_prices.get(self.region, {})
                        if region_data:
                            timestamp = region_data.get("timestamp")
                            if timestamp:
                                period_dt = self._parse_aemo_timestamp(timestamp)
                                if period_dt:
                                    self._current_period_end = self._get_next_period_boundary(period_dt)
                                    _LOGGER.info(
                                        "Period initialized from cached P5MIN: current=%s, next boundary=%s",
                                        timestamp,
                                        self._current_period_end.strftime("%H:%M:%S")
                                    )
                    
            except Exception as e:
                _LOGGER.error("Error fetching P5MIN: %s", e, exc_info=True)

            # Predispatch: Check on startup or every 5 minutes during active polling
            should_check_predispatch = (
                self._update_count == 1 or 
                (self._polling_mode == 'active' and self._active_polling_count == 1)
            )
            
            if should_check_predispatch:
                try:
                    predispatch_forecast, pd_file = await self._aemo_client.get_predispatch_forecast_with_file(
                        self.region, periods=96
                    )
                    
                    if pd_file and pd_file != self._last_predispatch_file:
                        _LOGGER.info("NEW Predispatch file: %s", pd_file)
                        self._last_predispatch_file = pd_file
                        data["predispatch_forecast"] = predispatch_forecast
                        _LOGGER.info("Updated predispatch: %d periods", len(predispatch_forecast))
                except Exception as e:
                    _LOGGER.error("Error fetching Predispatch: %s", e, exc_info=True)

            # If we found new data, log success and reset active polling counter
            if found_new_data:
                _LOGGER.info(
                    "✓ New data acquired - switching to WAIT mode until next period"
                )
                self._active_polling_count = 0

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