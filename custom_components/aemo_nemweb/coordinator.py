"""Data update coordinator — PRECISE PERIOD-ANCHORED POLLING.

Polling strategy
----------------
The coordinator runs in one of two modes, anchored to the moment the most
recent DISPATCH file was successfully received (T=0):

  SLOW mode  (T+0 → next_boundary + HUNT_START_OFFSET)
    update_interval = 60 s
    Each tick polls the DISPATCH *directory only* (cheap, ~5 KB).
    Also polls the P5MIN and Predispatch directory listings.
    ZIPs are only downloaded when a new filename is detected.

  HUNT mode  (next_boundary + HUNT_START_OFFSET → file found, or timeout)
    update_interval = 1 s
    Each tick calls poll_dispatch_directory() only.
    On a new filename  → fetch_dispatch_zip() → update sensors → SLOW mode.
    After HUNT_TIMEOUT → log warning, advance expected boundary by 5 min,
                         stay in SLOW mode until next boundary.

Timing constants
----------------
  HUNT_START_OFFSET =  15 s  (start 1-second polling 15 s into new period)
  HUNT_TIMEOUT      = 180 s  (give up after 3 minutes of 1-second polling)
  SLOW_INTERVAL     =  60 s  (poll interval outside the hunt window)

Example timeline
----------------
  12:05:03  New DISPATCH file received → T=0, SLOW mode
  12:05:03 … 12:10:15  SLOW ticks every 60 s (no new DISPATCH file expected)
  12:10:15  HUNT mode begins (15 s past the 12:10:00 boundary)
  12:10:15 … 12:10:38  1-second directory polls, no new file yet
  12:10:38  New filename detected → ZIP downloaded → sensors updated → SLOW
  12:10:38 … 12:15:15  SLOW mode again
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

# ---------------------------------------------------------------------------
# Timing constants — edit here if you need to tune behaviour
# ---------------------------------------------------------------------------
HUNT_START_OFFSET: int = 15    # seconds past boundary before 1 s polling starts
HUNT_TIMEOUT: int = 180        # seconds of 1 s polling before giving up
SLOW_INTERVAL: int = 60        # seconds between ticks in SLOW mode


class AEMOCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator with period-anchored two-mode polling."""

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
            update_interval=timedelta(seconds=1),
        )

        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._aemo_client: AEMOClient | None = None

        self.region: str = config.get(CONF_NEM_REGION, "NSW1")

        # ── Polling state ─────────────────────────────────────────────
        # 'slow'  — waiting for the next period boundary + offset
        # 'hunt'  — doing 1-second DISPATCH directory polls
        self._polling_mode: str = "slow"

        # Wall-clock time of the *start* of the period we currently hold
        # data for.  None until the first DISPATCH file is received.
        # Used to derive the next expected boundary.
        self._period_start: datetime | None = None

        # Wall-clock time when HUNT mode began.  Used for timeout tracking.
        self._hunt_start: datetime | None = None

        # ── File tracking ─────────────────────────────────────────────
        self._last_dispatch_file: str | None = None
        self._last_p5min_file: str | None = None
        self._last_predispatch_file: str | None = None

        # ── Misc state ────────────────────────────────────────────────
        self._dispatch_available: bool = False
        self._update_count: int = 0

        _LOGGER.info(
            "AEMO Coordinator initialised for %s "
            "(HUNT_START=%ds, HUNT_TIMEOUT=%ds, SLOW_INTERVAL=%ds)",
            self.region, HUNT_START_OFFSET, HUNT_TIMEOUT, SLOW_INTERVAL,
        )

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Create the HTTP session and AEMO client."""
        self._session = aiohttp.ClientSession()
        self._aemo_client = AEMOClient(self._session)
        _LOGGER.info("AEMO client ready")

    # ------------------------------------------------------------------
    # Timestamp helpers
    # ------------------------------------------------------------------

    def _parse_aemo_timestamp(self, timestamp_str: str) -> datetime | None:
        """Parse an AEMO period-ending timestamp to a naive local datetime.

        AEMO always publishes timestamps in AEST (UTC+10).  We convert to
        the local system timezone so comparisons with datetime.now() work
        correctly regardless of whether the host is in AEST or AEDT.
        """
        if not timestamp_str or "/" not in timestamp_str:
            return None
        try:
            from datetime import timezone, timedelta as _td
            dt_naive = datetime.strptime(timestamp_str, "%Y/%m/%d %H:%M:%S")
            aest = timezone(_td(hours=10))
            return dt_naive.replace(tzinfo=aest).astimezone().replace(tzinfo=None)
        except (ValueError, TypeError) as exc:
            _LOGGER.debug("Cannot parse AEMO timestamp %r: %s", timestamp_str, exc)
            return None

    def _next_boundary_after(self, t: datetime) -> datetime:
        """Return the next 5-minute clock boundary strictly after *t*."""
        # Truncate to the current 5-minute slot, then add 5 minutes
        slot_start = t.replace(
            minute=(t.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        return slot_start + timedelta(minutes=5)

    def _period_start_from_settlement(self, timestamp_str: str) -> datetime | None:
        """Derive the period-start boundary from an AEMO SETTLEMENTDATE.

        AEMO SETTLEMENTDATE is a period-END timestamp always on a 5-minute
        boundary (e.g. "2025/01/15 05:05:00" for the 05:00–05:05 period).
        Subtracting 5 minutes gives the exact period-start boundary, which
        is used as the anchor for the next HUNT window calculation.

        Returns a naive local-timezone datetime, or None on parse failure.
        """
        dt = self._parse_aemo_timestamp(timestamp_str)
        if dt is None:
            return None
        return dt - timedelta(minutes=5)

    # ------------------------------------------------------------------
    # Mode-transition logic — called once per tick, BEFORE any fetching
    # ------------------------------------------------------------------

    def _update_polling_mode(self) -> None:
        """Transition between SLOW and HUNT based on wall-clock time.

        This method only changes self._polling_mode and self.update_interval;
        it never fetches anything.

        Transition rules
        ----------------
        SLOW → HUNT  when  now >= next_boundary + HUNT_START_OFFSET
        HUNT → SLOW  when  new file found (handled in _async_update_data)
                     OR    hunt duration >= HUNT_TIMEOUT (handled here)
        """
        now = datetime.now()

        if self._polling_mode == "slow":
            if self._period_start is None:
                # No data yet — stay in SLOW but keep a short interval so we
                # pick up the very first file quickly.
                self.update_interval = timedelta(seconds=SLOW_INTERVAL)
                return

            next_boundary = self._next_boundary_after(self._period_start)
            hunt_open = next_boundary + timedelta(seconds=HUNT_START_OFFSET)

            if now >= hunt_open:
                self._polling_mode = "hunt"
                self._hunt_start = now
                self.update_interval = timedelta(seconds=1)
                _LOGGER.info(
                    "→ HUNT mode: boundary was %s, started polling at %s",
                    next_boundary.strftime("%H:%M:%S"),
                    now.strftime("%H:%M:%S"),
                )
            else:
                self.update_interval = timedelta(seconds=SLOW_INTERVAL)

        elif self._polling_mode == "hunt":
            if self._hunt_start is None:
                # Shouldn't happen, but be safe
                self._hunt_start = now

            hunt_elapsed = (now - self._hunt_start).total_seconds()

            if hunt_elapsed >= HUNT_TIMEOUT:
                # Gave up waiting — advance the period clock by exactly 5
                # minutes so we open the next HUNT window at the right time.
                _LOGGER.warning(
                    "HUNT timed out after %ds with no new DISPATCH file — "
                    "advancing period clock by 5 minutes",
                    int(hunt_elapsed),
                )
                if self._period_start is not None:
                    self._period_start += timedelta(minutes=5)
                self._polling_mode = "slow"
                self._hunt_start = None
                self.update_interval = timedelta(seconds=SLOW_INTERVAL)

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data according to the current polling mode.

        Returns a new dict every call so Home Assistant always detects a
        change and propagates it to sensor entities.
        """
        try:
            self._update_count += 1

            if self._aemo_client is None:
                await self._async_setup()

            # Decide mode *before* fetching
            self._update_polling_mode()

            # Seed the return dict with whatever we already hold
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
                data.update({
                    k: self.data[k]
                    for k in data
                    if k in self.data
                })

            # ----------------------------------------------------------
            # HUNT mode — cheap 1-second directory poll only
            # ----------------------------------------------------------
            if self._polling_mode == "hunt":
                await self._hunt_tick(data)
                return data

            # ----------------------------------------------------------
            # SLOW mode — 60-second maintenance poll
            # ----------------------------------------------------------
            await self._slow_tick(data)
            return data

        except Exception as err:
            _LOGGER.error("Error in update cycle: %s", err, exc_info=True)
            raise UpdateFailed(f"Error fetching AEMO data: {err}") from err

    # ------------------------------------------------------------------
    # HUNT tick
    # ------------------------------------------------------------------

    async def _hunt_tick(self, data: dict[str, Any]) -> None:
        """One 1-second DISPATCH directory poll during HUNT mode.

        On a new filename: download ZIP, update data, transition to SLOW.
        """
        filename = await self._aemo_client.poll_dispatch_directory()

        if not filename or filename == self._last_dispatch_file:
            # No new file yet — nothing to do this tick
            return

        # ── New file detected ──────────────────────────────────────────
        _LOGGER.info(
            "✓ New DISPATCH file detected after %ds of HUNT: %s",
            int((datetime.now() - self._hunt_start).total_seconds())
            if self._hunt_start else 0,
            filename,
        )

        prices, demands = await self._aemo_client.fetch_dispatch_zip(filename)
        if not prices:
            _LOGGER.warning("DISPATCH ZIP parsed empty prices for %s", filename)
            return

        self._last_dispatch_file = filename
        self._dispatch_available = True

        region_data = prices.get(self.region, {})
        demand_data = demands.get(self.region, {})

        if region_data:
            data["realtime_price"] = region_data
            data["realtime_demand"] = demand_data if demand_data else region_data
            data["last_update"] = region_data.get("timestamp")
            data["spike_info"] = self._aemo_client.calculate_spike_info(
                region_data.get("price_mwh", 0) or 0
            )

            # Anchor the period clock to the AEMO SETTLEMENTDATE (period-end
            # boundary) so the next HUNT window opens at exactly
            # SETTLEMENTDATE + HUNT_START_OFFSET, regardless of how many
            # seconds after the boundary this file was received.
            settlement_ts = region_data.get("timestamp", "")
            period_start = self._period_start_from_settlement(settlement_ts)
            if period_start is not None:
                self._period_start = period_start
            else:
                # Fallback: wall-clock minus a conservative offset
                _LOGGER.warning(
                    "Could not parse SETTLEMENTDATE %r — using wall-clock fallback",
                    settlement_ts,
                )
                self._period_start = datetime.now() - timedelta(seconds=30)

            _LOGGER.info(
                "DISPATCH price for %s: $%.4f/kWh  ts=%s  "
                "(spike=%s, ratio=%.2fx)",
                self.region,
                region_data.get("price_dollars", 0),
                region_data.get("timestamp", "?"),
                "YES" if data["spike_info"].get("is_spike") else "no",
                data["spike_info"].get("spike_ratio", 1.0),
            )

        # Transition to SLOW — next HUNT window opens at next boundary+15s
        self._polling_mode = "slow"
        self._hunt_start = None
        self.update_interval = timedelta(seconds=SLOW_INTERVAL)
        next_boundary = self._next_boundary_after(self._period_start)
        hunt_open = next_boundary + timedelta(seconds=HUNT_START_OFFSET)
        _LOGGER.info(
            "→ SLOW mode  next HUNT opens at %s",
            hunt_open.strftime("%H:%M:%S"),
        )

    # ------------------------------------------------------------------
    # SLOW tick
    # ------------------------------------------------------------------

    async def _slow_tick(self, data: dict[str, Any]) -> None:
        """60-second maintenance poll.

        Checks all three directory listings.  Downloads a ZIP only when a
        new filename appears.  This keeps the SLOW tick cheap in the common
        case (three tiny HTTP GETs, no ZIP parsing).
        """
        # ── 1. DISPATCH directory — for period-boundary initialisation
        #       and as a safety net in case a HUNT window was missed ───
        await self._slow_dispatch(data)

        # ── 2. P5MIN actual + short forecast ──────────────────────────
        await self._slow_p5min(data)

        # ── 3. Predispatch (30-minute forecast) ───────────────────────
        await self._slow_predispatch(data)

    async def _slow_dispatch(self, data: dict[str, Any]) -> None:
        """Check DISPATCH directory during SLOW mode.

        Normally the HUNT window handles new DISPATCH files.  This catches:
          - the very first file on startup (no period_start yet)
          - any file that appeared while HA was down between periods
        """
        try:
            filename = await self._aemo_client.poll_dispatch_directory()
            if not filename:
                return

            if filename == self._last_dispatch_file:
                # Already have this file — use it only to seed period_start
                # on first run if we have no period clock yet.
                if self._period_start is None and self.data:
                    cached = (self.data.get("realtime_price") or {})
                    ts = cached.get("timestamp", "")
                    if ts:
                        period_start = self._period_start_from_settlement(ts)
                        if period_start is not None:
                            self._period_start = period_start
                            _LOGGER.info(
                                "Period clock seeded from cached DISPATCH ts: %s → "
                                "next boundary %s",
                                ts,
                                self._next_boundary_after(
                                    self._period_start
                                ).strftime("%H:%M:%S"),
                            )
                return

            # New file — download and parse
            _LOGGER.info(
                "SLOW tick: new DISPATCH file %s (missed by HUNT?)", filename
            )
            prices, demands = await self._aemo_client.fetch_dispatch_zip(filename)
            if not prices:
                return

            self._last_dispatch_file = filename
            self._dispatch_available = True

            region_data = prices.get(self.region, {})
            demand_data = demands.get(self.region, {})

            if region_data:
                data["realtime_price"] = region_data
                data["realtime_demand"] = demand_data if demand_data else region_data
                data["last_update"] = region_data.get("timestamp")
                data["spike_info"] = self._aemo_client.calculate_spike_info(
                    region_data.get("price_mwh", 0) or 0
                )
                settlement_ts = region_data.get("timestamp", "")
                period_start = self._period_start_from_settlement(settlement_ts)
                if period_start is not None:
                    self._period_start = period_start
                else:
                    self._period_start = datetime.now() - timedelta(seconds=30)

                _LOGGER.info(
                    "SLOW-DISPATCH price for %s: $%.4f/kWh  ts=%s",
                    self.region,
                    region_data.get("price_dollars", 0),
                    region_data.get("timestamp", "?"),
                )

        except Exception as exc:
            _LOGGER.debug("SLOW DISPATCH poll failed: %s", exc)

    async def _slow_p5min(self, data: dict[str, Any]) -> None:
        """Check P5MIN directory and download ZIP only on new filename."""
        try:
            p5min_prices, p5min_file = (
                await self._aemo_client.get_current_prices_with_file()
            )

            if not p5min_file:
                return

            if p5min_file == self._last_p5min_file:
                # Seed period clock from cached P5MIN if we have nothing else
                if self._period_start is None:
                    region_data = p5min_prices.get(self.region, {})
                    ts = region_data.get("timestamp", "")
                    if ts:
                        period_end_dt = self._parse_aemo_timestamp(ts)
                        if period_end_dt:
                            # AEMO timestamps are period-END; subtract 5 min
                            # to get period-start for boundary arithmetic.
                            self._period_start = period_end_dt - timedelta(minutes=5)
                            _LOGGER.info(
                                "Period clock seeded from cached P5MIN ts: %s → "
                                "next boundary %s",
                                ts,
                                self._next_boundary_after(
                                    self._period_start
                                ).strftime("%H:%M:%S"),
                            )
                return

            # New P5MIN file
            _LOGGER.info("NEW P5MIN file: %s", p5min_file)
            self._last_p5min_file = p5min_file

            region_data = p5min_prices.get(self.region, {})
            if region_data:
                data["spot_price"] = region_data
                if not data["last_update"]:
                    data["last_update"] = region_data.get("timestamp")

                if self._period_start is None:
                    ts = region_data.get("timestamp", "")
                    if ts:
                        period_end_dt = self._parse_aemo_timestamp(ts)
                        if period_end_dt:
                            self._period_start = period_end_dt - timedelta(minutes=5)

                _LOGGER.info(
                    "P5MIN spot for %s: $%.4f/kWh  ts=%s",
                    self.region,
                    region_data.get("price_dollars", 0),
                    region_data.get("timestamp", "?"),
                )

            # Fetch 5-min forecast from same ZIP
            p5min_forecast = await self._aemo_client.get_p5min_forecast(
                self.region, periods=12
            )
            data["p5min_forecast"] = p5min_forecast
            _LOGGER.info("5-min forecast: %d periods", len(p5min_forecast))

        except Exception as exc:
            _LOGGER.error("P5MIN poll failed: %s", exc, exc_info=True)

    async def _slow_predispatch(self, data: dict[str, Any]) -> None:
        """Check Predispatch directory and download ZIP only on new filename."""
        try:
            forecasts, pd_file = (
                await self._aemo_client.get_predispatch_forecast_with_file(
                    self.region, periods=96
                )
            )

            if not pd_file or pd_file == self._last_predispatch_file:
                return

            _LOGGER.info("NEW Predispatch file: %s  (%d periods)", pd_file, len(forecasts))
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