"""Data update coordinator — PERIOD-ANCHORED DASHBOARD POLLING.

Polling strategy
----------------
Realtime price and demand are fetched from AEMO's public dashboard JSON API
(``/NEM/v1/PWS/NEMDashboard/elecSummary``).  The coordinator runs in two
modes anchored to the 5-minute DISPATCH period boundaries:

  WAIT mode  (period confirmed → next boundary)
    update_interval = WAIT_INTERVAL (60 s)
    A single dashboard call per tick to keep cached values fresh.
    No new period is expected yet so fast polling would be wasted.

  HUNT mode  (next boundary → new period found, or timeout)
    update_interval = HUNT_INTERVAL (5 s)
    Polls the dashboard every 5 s waiting for settlementDate to advance.
    On a new period  → update sensors, fetch forecast ZIPs, → WAIT mode.
    After HUNT_TIMEOUT with no new period → advance expected boundary by
    5 minutes, log a warning, return to WAIT mode.

Timing constants
----------------
  HUNT_START_OFFSET =  5 s   HUNT begins exactly on the period boundary
  HUNT_TIMEOUT      = 90 s   give up after 90 s of fast polling
  HUNT_INTERVAL     = 5 s   poll interval during HUNT mode
  WAIT_INTERVAL     = 60 s   poll interval during WAIT mode

Example timeline
----------------
  12:05:08  Dashboard returns settlementDate 12:05:00 → new period confirmed
            → forecast ZIPs fetched, mode → WAIT
  12:05:08 … 12:10:00  WAIT ticks every 60 s (4 cheap dashboard calls)
  12:10:00  HUNT mode begins exactly on the boundary
  12:10:00  Dashboard still shows 12:05:00 → waiting…
  12:10:10  Dashboard still shows 12:05:00 → waiting…
  12:10:20  Dashboard returns settlementDate 12:10:00 → new period!
            → forecast ZIPs fetched, mode → WAIT until 12:15:00
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aemo_client import AEMOClient
from .const import (
    CONF_NEM_REGION,
    DASHBOARD_POLL_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------
HUNT_START_OFFSET: int = 5    # seconds past boundary before HUNT begins
HUNT_TIMEOUT: int = 90        # seconds of HUNT polling before giving up
HUNT_INTERVAL: int = DASHBOARD_POLL_INTERVAL   # fast poll interval (5 s)
WAIT_INTERVAL: int = 60       # slow poll interval between periods


class AEMOCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator with period-anchored two-mode dashboard polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=HUNT_INTERVAL),
        )

        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._aemo_client: AEMOClient | None = None

        self.region: str = config.get(CONF_NEM_REGION, "NSW1")

        # ── File tracking (ZIPs only fetched when filename changes) ───
        self._last_p5min_file: str | None = None
        self._last_predispatch_file: str | None = None

        # ── Period clock ──────────────────────────────────────────────
        # AEMO SETTLEMENTDATE of the period we currently hold data for.
        # None until the first successful dashboard fetch.
        self._last_settlement_date: str | None = None

        # Wall-clock time of the period-start boundary we currently hold
        # (= settlementDate − 5 min, in local time).  Used to derive the
        # next HUNT window open time.
        self._period_start: datetime | None = None

        # ── Mode state ────────────────────────────────────────────────
        # 'hunt'  — polling every HUNT_INTERVAL seconds for a new period
        # 'wait'  — polling every WAIT_INTERVAL seconds between periods
        self._polling_mode: str = "hunt"   # start in HUNT to grab data fast
        self._hunt_start: datetime | None = None

        # ── Misc ──────────────────────────────────────────────────────
        self._dispatch_available: bool = False
        self._update_count: int = 0

        _LOGGER.info(
            "AEMO Coordinator initialised for %s "
            "(HUNT every %ds, WAIT every %ds, HUNT_START+%ds, timeout %ds)",
            self.region, HUNT_INTERVAL, WAIT_INTERVAL,
            HUNT_START_OFFSET, HUNT_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Create the HTTP session and AEMO client."""
        self._session = aiohttp.ClientSession()
        self._aemo_client = AEMOClient(self._session)
        _LOGGER.info("AEMO client ready (period-anchored dashboard mode)")

    # ------------------------------------------------------------------
    # Period-clock helpers
    # ------------------------------------------------------------------

    def _settlement_to_period_start(self, settlement_ts: str) -> datetime | None:
        """Convert an AEMO-style timestamp to a naive local period-start datetime.

        AEMO timestamps are period-END boundaries in AEST (UTC+10), e.g.
        "2026/05/24 09:25:00" represents the 09:20–09:25 period.
        Subtracting 5 minutes gives the period-start boundary used for
        computing when the next HUNT window should open.

        Returns a naive local-timezone datetime, or None on parse failure.
        """
        if not settlement_ts or "/" not in settlement_ts:
            return None
        try:
            aest = timezone(timedelta(hours=10))
            dt_naive = datetime.strptime(settlement_ts, "%Y/%m/%d %H:%M:%S")
            dt_aest = dt_naive.replace(tzinfo=aest)
            # Convert to local time, strip tzinfo for comparison with datetime.now()
            dt_local = dt_aest.astimezone().replace(tzinfo=None)
            return dt_local - timedelta(minutes=5)
        except (ValueError, TypeError) as exc:
            _LOGGER.debug("Cannot parse AEMO timestamp %r: %s", settlement_ts, exc)
            return None

    def _next_boundary(self, period_start: datetime) -> datetime:
        """Return the next 5-minute boundary strictly after period_start."""
        slot = period_start.replace(
            minute=(period_start.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        return slot + timedelta(minutes=5)

    # ------------------------------------------------------------------
    # Mode-transition logic
    # ------------------------------------------------------------------

    def _update_polling_mode(self, now: datetime) -> None:
        """Transition between WAIT and HUNT modes and adjust update_interval.

        Called at the start of every tick, before any network I/O.

        WAIT → HUNT  when  now >= next_boundary + HUNT_START_OFFSET
        HUNT → WAIT  handled in _async_update_data after new period confirmed
                     OR here on HUNT_TIMEOUT
        """
        if self._polling_mode == "wait":
            if self._period_start is None:
                # No period clock yet — stay in HUNT until first data arrives
                return

            hunt_open = (
                self._next_boundary(self._period_start)
                + timedelta(seconds=HUNT_START_OFFSET)
            )
            if now >= hunt_open:
                self._polling_mode = "hunt"
                self._hunt_start = now
                self.update_interval = timedelta(seconds=HUNT_INTERVAL)
                _LOGGER.info(
                    "→ HUNT mode  (boundary was %s, HUNT opened at %s)",
                    self._next_boundary(self._period_start).strftime("%H:%M:%S"),
                    now.strftime("%H:%M:%S"),
                )

        elif self._polling_mode == "hunt":
            if self._hunt_start is None:
                self._hunt_start = now

            elapsed = (now - self._hunt_start).total_seconds()
            if elapsed >= HUNT_TIMEOUT:
                _LOGGER.warning(
                    "HUNT timed out after %ds — no new period found; "
                    "advancing period clock by 5 minutes",
                    int(elapsed),
                )
                if self._period_start is not None:
                    self._period_start += timedelta(minutes=5)
                self._enter_wait_mode(now)

    def _enter_wait_mode(self, now: datetime) -> None:
        """Switch to WAIT mode and log the next HUNT window open time."""
        self._polling_mode = "wait"
        self._hunt_start = None
        self.update_interval = timedelta(seconds=WAIT_INTERVAL)

        if self._period_start is not None:
            hunt_open = (
                self._next_boundary(self._period_start)
                + timedelta(seconds=HUNT_START_OFFSET)
            )
            _LOGGER.info(
                "→ WAIT mode  next HUNT opens at %s",
                hunt_open.strftime("%H:%M:%S"),
            )

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data each tick according to the current polling mode."""
        try:
            self._update_count += 1
            now = datetime.now()

            if self._aemo_client is None:
                await self._async_setup()

            # Decide mode before fetching
            self._update_polling_mode(now)

            # Carry forward whatever we already hold
            data: dict[str, Any] = {
                "last_update": None,
                "realtime_price": None,
                "realtime_demand": None,
                "spot_price": None,
                "p5min_forecast": [],
                "predispatch_forecast": [],
                "spike_info": {},
            }
            if self.data:
                data.update({k: self.data[k] for k in data if k in self.data})

            # Always call the dashboard — it's cheap (~30 ms / ~4 KB)
            new_period = await self._fetch_dashboard(data)

            if new_period:
                # New 5-minute period confirmed — fetch forecast ZIPs then
                # drop back to WAIT until the next boundary.
                await self._fetch_p5min(data)
                await self._fetch_predispatch(data)
                self._enter_wait_mode(now)

            return data

        except Exception as err:
            _LOGGER.error("Error in update cycle: %s", err, exc_info=True)
            raise UpdateFailed(f"Error fetching AEMO data: {err}") from err

    # ------------------------------------------------------------------
    # Dashboard fetch
    # ------------------------------------------------------------------

    async def _fetch_dashboard(self, data: dict[str, Any]) -> bool:
        """Call the AEMO dashboard JSON API and update realtime price/demand.

        Returns True when the settlementDate has advanced (new period), so
        the caller knows to trigger forecast ZIP fetches and enter WAIT mode.
        """
        try:
            all_regions = await self._aemo_client.fetch_dashboard_summary()
        except Exception as exc:
            _LOGGER.debug("Dashboard fetch error: %s", exc)
            return False

        if not all_regions:
            _LOGGER.debug("Dashboard returned no data — retaining cached values")
            return False

        region_data = all_regions.get(self.region)
        if not region_data:
            _LOGGER.warning(
                "Dashboard response did not include region %s (available: %s)",
                self.region, list(all_regions.keys()),
            )
            return False

        self._dispatch_available = True

        data["realtime_price"] = {
            "price_mwh": region_data["price_mwh"],
            "price_cents": region_data["price_cents"],
            "price_dollars": region_data["price_dollars"],
            "timestamp": region_data["timestamp"],
            "price_status": region_data["price_status"],
        }
        data["realtime_demand"] = {
            "demand_mw": region_data["demand_mw"],
            "timestamp": region_data["timestamp"],
            "net_interchange_mw": region_data["net_interchange_mw"],
            "scheduled_generation_mw": region_data["scheduled_generation_mw"],
            "semischeduled_generation_mw": region_data["semischeduled_generation_mw"],
        }
        data["last_update"] = region_data["timestamp"]
        data["spike_info"] = self._aemo_client.calculate_spike_info(
            region_data["price_mwh"]
        )

        # Detect period advance
        current_ts = region_data["timestamp"]
        if current_ts == self._last_settlement_date:
            _LOGGER.debug(
                "Dashboard: %s  $%.4f/kWh  %.1f MW  (same period %s)",
                self.region,
                region_data["price_dollars"],
                region_data["demand_mw"],
                current_ts,
            )
            return False

        # New period!
        _LOGGER.info(
            "New DISPATCH period: %s → %s  $%.4f/kWh  %.1f MW  status=%s",
            self._last_settlement_date or "(startup)",
            current_ts,
            region_data["price_dollars"],
            region_data["demand_mw"],
            region_data["price_status"],
        )
        self._last_settlement_date = current_ts
        self._period_start = self._settlement_to_period_start(current_ts)
        if self._period_start is None:
            # Fallback: anchor to wall-clock
            self._period_start = datetime.now() - timedelta(seconds=30)

        return True

    # ------------------------------------------------------------------
    # Forecast ZIP fetches (only on new period)
    # ------------------------------------------------------------------

    async def _fetch_p5min(self, data: dict[str, Any]) -> None:
        """Fetch P5MIN ZIP only when a new file appears in the directory."""
        try:
            p5min_prices, p5min_file = (
                await self._aemo_client.get_current_prices_with_file()
            )
            if not p5min_file or p5min_file == self._last_p5min_file:
                _LOGGER.debug("P5MIN: no new file (%s)", p5min_file or "none")
                return

            _LOGGER.info("NEW P5MIN file: %s", p5min_file)
            self._last_p5min_file = p5min_file

            region_data = p5min_prices.get(self.region, {})
            if region_data:
                data["spot_price"] = region_data
                _LOGGER.info(
                    "P5MIN spot for %s: $%.4f/kWh  ts=%s",
                    self.region,
                    region_data.get("price_dollars", 0),
                    region_data.get("timestamp", "?"),
                )

            p5min_forecast = await self._aemo_client.get_p5min_forecast(
                self.region, periods=12
            )
            data["p5min_forecast"] = p5min_forecast
            _LOGGER.info("5-min forecast: %d periods", len(p5min_forecast))

        except Exception as exc:
            _LOGGER.error("P5MIN poll failed: %s", exc, exc_info=True)

    async def _fetch_predispatch(self, data: dict[str, Any]) -> None:
        """Fetch Predispatch ZIP only when a new file appears."""
        try:
            forecasts, pd_file = (
                await self._aemo_client.get_predispatch_forecast_with_file(
                    self.region, periods=96
                )
            )
            if not pd_file or pd_file == self._last_predispatch_file:
                return

            _LOGGER.info(
                "NEW Predispatch file: %s  (%d periods)", pd_file, len(forecasts)
            )
            self._last_predispatch_file = pd_file
            data["predispatch_forecast"] = forecasts

        except Exception as exc:
            _LOGGER.error("Predispatch poll failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Close the HTTP session."""
        _LOGGER.info("Shutting down AEMO coordinator")
        if self._session:
            await self._session.close()
            self._session = None