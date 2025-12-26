"""AEMO API client - ENHANCED WITH REAL-TIME DISPATCH MONITORING.

Monitors DISPATCH files every 30 seconds for real-time spike detection.
No battery automations - just provides data for user decisions.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import zipfile
from datetime import datetime
from typing import Any

import aiohttp

from .const import (
    AEMO_P5MIN_ACTUAL_URL,
    AEMO_DISPATCH_URL,
    AEMO_PREDISPATCH_BASE_URL,
    NEM_REGIONS,
)

_LOGGER = logging.getLogger(__name__)


class AEMOClient:
    """Client for fetching AEMO wholesale electricity prices."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the AEMO client."""
        self._session = session
        self._p5min_cache: dict[str, Any] = {}
        self._dispatch_cache: dict[str, Any] = {}
        self._predispatch_cache: dict[str, Any] = {}
        
        # For spike detection
        self._price_history: list[float] = []  # Last 12 prices (1 hour)

    async def get_dispatch_price_with_file(self) -> tuple[dict[str, dict[str, Any]], str]:
        """Fetch real-time dispatch price (updated every ~2-3 minutes).
        
        This is faster than P5MIN files and gives near real-time prices.
        """
        try:
            async with self._session.get(
                AEMO_DISPATCH_URL,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    return {}, ""
                html = await response.text()

            # Pattern for DispatchIS files  
            # Files are like: PUBLIC_DISPATCHIS_202512251520_0000000495664033.zip
            # Format: PUBLIC_DISPATCHIS_{timestamp}_{sequence}.zip
            pattern = r'PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip'
            all_matches = re.findall(pattern, html)
            
            if not all_matches:
                _LOGGER.debug("No DISPATCHIS files found, trying alternative pattern")
                # Try broader pattern for any dispatch-related files
                pattern = r'PUBLIC_DISPATCH[A-Z]*_(\d{12})_\d+\.zip'
                all_matches = re.findall(pattern, html)
                
            if not all_matches:
                return {}, ""

            latest_timestamp = sorted(all_matches)[-1]
            
            # Find the actual filename - DispatchIS format with sequence number
            # Format: PUBLIC_DISPATCHIS_{timestamp}_{sequence}.zip
            latest_pattern = f'PUBLIC_DISPATCHIS_{latest_timestamp}_\\d+\\.zip'
            latest_files = re.findall(latest_pattern, html)
            
            if not latest_files:
                # Try broader pattern for any dispatch file with this timestamp
                latest_pattern = f'PUBLIC_DISPATCH[A-Z]*_{latest_timestamp}_\\d+\\.zip'
                latest_files = re.findall(latest_pattern, html)
            
            if not latest_files:
                return {}, ""
            
            latest_file = latest_files[0]
            
            # Check cache
            if latest_file in self._dispatch_cache:
                _LOGGER.debug("Using cached DISPATCH data for %s", latest_file)
                return self._dispatch_cache[latest_file], latest_file
            
            file_url = f"{AEMO_DISPATCH_URL}{latest_file}"
            _LOGGER.info("Downloading NEW DISPATCH file: %s", latest_file)

            async with self._session.get(
                file_url,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    return {}, ""
                content = await response.read()

            prices = self._parse_dispatch_zip(content)
            self._dispatch_cache = {latest_file: prices}
            
            return prices, latest_file

        except Exception as e:
            _LOGGER.debug("Error fetching DISPATCH (expected if files not available): %s", e)
            return {}, ""

    def _parse_dispatch_zip(self, content: bytes) -> dict[str, dict[str, Any]]:
        """Parse DispatchIS ZIP for current regional prices.
        
        DispatchIS files contain DISPATCH.REGIONSUM tables with regional price data.
        """
        prices = {}

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for filename in zf.namelist():
                    if filename.upper().endswith('.CSV'):
                        with zf.open(filename) as f:
                            csv_content = f.read().decode("utf-8")
                            reader = csv.reader(io.StringIO(csv_content))

                            # Track if we found a header to understand column positions
                            header_cols = {}
                            
                            for row in reader:
                                if not row or len(row) < 8:
                                    continue
                                
                                # Check for header row - be more flexible in detection
                                if row[0] == "C":
                                    # Check if this row contains REGIONSUM-related columns
                                    row_str = ",".join(row).upper()
                                    if "REGIONSUM" in row_str or ("REGIONID" in row_str and ("RRP" in row_str or "PRICE" in row_str)):
                                        _LOGGER.warning("DISPATCH HEADER ROW: %s", row[:20])  # First 20 columns
                                        # Parse header to find column positions
                                        for i, col in enumerate(row):
                                            col_upper = col.strip().strip('"').upper()
                                            if col_upper == "REGIONID":
                                                header_cols["regionid"] = i
                                            elif col_upper == "RRP":
                                                header_cols["rrp"] = i
                                            elif col_upper in ("PRICE", "CLEAREDMW"):  # Try other names
                                                if "rrp" not in header_cols:
                                                    header_cols["rrp"] = i
                                            elif col_upper in ("SETTLEMENTDATE", "DATETIME", "PERIODID"):
                                                header_cols["datetime"] = i
                                        _LOGGER.warning("Found REGIONSUM header, columns: regionid=%s, rrp=%s, datetime=%s",
                                                    header_cols.get("regionid"), header_cols.get("rrp"), header_cols.get("datetime"))
                                        continue

                                # Skip non-data rows
                                if row[0] in ('I', 'C'):
                                    continue

                                # Look for DISPATCH.REGIONSUM or similar data rows
                                if row[0] == "D" and len(row) > 2:
                                    table_name = f"{row[1]}.{row[2]}" if len(row) > 2 else ""
                                    
                                    # Log all table names we see for debugging (only once per table)
                                    if table_name not in getattr(self, '_seen_dispatch_tables', set()):
                                        if not hasattr(self, '_seen_dispatch_tables'):
                                            self._seen_dispatch_tables = set()
                                        self._seen_dispatch_tables.add(table_name)
                                        _LOGGER.info("Found DISPATCH table: %s (columns: %d)", table_name, len(row))
                                    
                                    # Look for DISPATCH.PRICE table (has RRP column)
                                    if table_name.upper() == "DISPATCH.PRICE":
                                        try:
                                            # DISPATCH.PRICE format:
                                            # D, DISPATCH, PRICE, 5, SETTLEMENTDATE, RUNNO, REGIONID, DISPATCHINTERVAL, INTERVENTION, RRP, ...
                                            # Indices: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9
                                            
                                            if len(row) > 9:
                                                regionid = row[6].strip().strip('"')
                                                
                                                if regionid in NEM_REGIONS:
                                                    settlementdate = row[4].strip().strip('"')
                                                    intervention = int(row[8].strip()) if row[8].strip().isdigit() else 0
                                                    rrp = float(row[9].strip())
                                                    
                                                    # Only use intervention=0 (normal market prices)
                                                    if intervention == 0:
                                                        _LOGGER.info(
                                                            "DISPATCH.PRICE: %s at %s = $%.2f/MWh (intervention=%d)",
                                                            regionid, settlementdate, rrp, intervention
                                                        )
                                                        
                                                        prices[regionid] = {
                                                            "price_mwh": rrp,
                                                            "price_cents": rrp / 10,
                                                            "price_dollars": rrp / 1000,
                                                            "timestamp": settlementdate,
                                                        }
                                        except (ValueError, IndexError) as e:
                                            _LOGGER.debug("Parse error in DISPATCH.PRICE row: %s", e)
                                            continue

            if not prices:
                _LOGGER.warning("No prices extracted from DISPATCH file. Header cols: %s", header_cols)
            
            return prices

        except Exception as e:
            _LOGGER.error("Error parsing DISPATCH ZIP: %s", e, exc_info=True)
            return {}

    async def get_current_prices_with_file(self) -> tuple[dict[str, dict[str, Any]], str]:
        """Fetch ACTUAL current prices from P5MIN (most recent completed period)."""
        try:
            async with self._session.get(
                AEMO_P5MIN_ACTUAL_URL,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    return {}, ""
                html = await response.text()

            pattern = r'PUBLIC_P5MIN_(\d{12})_\d{14}\.zip'
            all_matches = re.findall(pattern, html)
            
            if not all_matches:
                return {}, ""

            latest_timestamp = sorted(all_matches)[-1]
            latest_pattern = f'PUBLIC_P5MIN_{latest_timestamp}_\\d{{14}}\\.zip'
            latest_files = re.findall(latest_pattern, html)
            
            if not latest_files:
                return {}, ""
            
            latest_file = latest_files[0]
            
            if latest_file in self._p5min_cache:
                _LOGGER.debug("Using cached P5MIN data for %s", latest_file)
                return self._p5min_cache[latest_file], latest_file
            
            file_url = f"{AEMO_P5MIN_ACTUAL_URL}{latest_file}"
            _LOGGER.info("Downloading NEW P5MIN file: %s", latest_file)

            async with self._session.get(
                file_url,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    return {}, ""
                content = await response.read()

            prices = self._parse_p5min_actual(content)
            self._p5min_cache = {latest_file: prices}
            
            return prices, latest_file

        except Exception as e:
            _LOGGER.error("Error fetching P5MIN: %s", e, exc_info=True)
            return {}, ""

    def _parse_p5min_actual(self, content: bytes) -> dict[str, dict[str, Any]]:
        """Parse P5MIN for ACTUAL prices (earliest/completed period)."""
        prices = {}

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for filename in zf.namelist():
                    if filename.upper().endswith('.CSV'):
                        with zf.open(filename) as f:
                            csv_content = f.read().decode("utf-8")
                            reader = csv.reader(io.StringIO(csv_content))

                            all_rows = []
                            run_datetime = None

                            for row in reader:
                                if not row or row[0] in ('I', 'C') or len(row) < 9:
                                    continue

                                if row[0] == "D" and len(row) > 2 and row[1] == "P5MIN" and row[2] == "REGIONSOLUTION":
                                    try:
                                        if run_datetime is None:
                                            run_datetime = row[4].strip().strip('"')
                                        
                                        intervention = row[5].strip().strip('"')
                                        if intervention != "0":
                                            continue
                                        
                                        periodid = row[6].strip().strip('"')
                                        regionid = row[7].strip().strip('"')
                                        rrp = float(row[8].strip())
                                        
                                        if regionid in NEM_REGIONS:
                                            all_rows.append({
                                                "region": regionid,
                                                "periodid": periodid,
                                                "rrp": rrp,
                                            })
                                    except (ValueError, IndexError):
                                        continue

                            # Get EARLIEST period = most recent actual
                            if all_rows:
                                for region_code in NEM_REGIONS:
                                    region_rows = [r for r in all_rows if r["region"] == region_code]
                                    
                                    if not region_rows:
                                        continue
                                    
                                    actual_row = min(region_rows, key=lambda x: x["periodid"])
                                    
                                    prices[region_code] = {
                                        "price_mwh": actual_row["rrp"],
                                        "price_cents": actual_row["rrp"] / 10,
                                        "price_dollars": actual_row["rrp"] / 1000,
                                        "timestamp": actual_row["periodid"],
                                    }

            return prices

        except Exception as e:
            _LOGGER.error("Error parsing P5MIN: %s", e, exc_info=True)
            return {}

    async def get_p5min_forecast(
        self, region: str, periods: int = 12
    ) -> list[dict[str, Any]]:
        """Get 5-min FORECAST prices."""
        try:
            async with self._session.get(
                AEMO_P5MIN_ACTUAL_URL,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    return []
                html = await response.text()

            pattern = r'PUBLIC_P5MIN_(\d{12})_\d{14}\.zip'
            all_matches = re.findall(pattern, html)
            
            if not all_matches:
                return []

            latest_timestamp = sorted(all_matches)[-1]
            latest_pattern = f'PUBLIC_P5MIN_{latest_timestamp}_\\d{{14}}\\.zip'
            latest_files = re.findall(latest_pattern, html)
            
            if not latest_files:
                return []
            
            latest_file = latest_files[0]
            file_url = f"{AEMO_P5MIN_ACTUAL_URL}{latest_file}"

            async with self._session.get(
                file_url,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    return []
                content = await response.read()

            return self._parse_p5min_forecast(content, region, periods)

        except Exception as e:
            _LOGGER.error("Error fetching forecast: %s", e, exc_info=True)
            return []

    def _parse_p5min_forecast(
        self, content: bytes, region: str, periods: int
    ) -> list[dict[str, Any]]:
        """Parse FORECAST prices (skip first/actual period)."""
        forecasts = []

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                _LOGGER.debug("P5MIN ZIP contains files: %s", zf.namelist())
                for filename in zf.namelist():
                    if filename.upper().endswith('.CSV'):
                        with zf.open(filename) as f:
                            csv_content = f.read().decode("utf-8")
                            reader = csv.reader(io.StringIO(csv_content))

                            all_rows = []
                            row_count = 0
                            regionsolution_count = 0
                            
                            for row in reader:
                                row_count += 1
                                if not row or row[0] in ('I', 'C') or len(row) < 9:
                                    continue

                                if row[0] == "D" and len(row) > 2 and row[1] == "P5MIN" and row[2] == "REGIONSOLUTION":
                                    regionsolution_count += 1
                                    try:
                                        intervention = row[5].strip().strip('"')
                                        if intervention != "0":
                                            continue
                                        
                                        periodid = row[6].strip().strip('"')
                                        regionid = row[7].strip().strip('"')
                                        rrp = float(row[8].strip())
                                        
                                        if regionid == region:
                                            all_rows.append({
                                                "timestamp": periodid,
                                                "price_mwh": rrp,
                                                "price_cents": rrp / 10,
                                                "price_dollars": rrp / 1000,
                                            })
                                    except (ValueError, IndexError) as e:
                                        _LOGGER.debug("Error parsing P5MIN row: %s", e)
                                        continue

            _LOGGER.debug("P5MIN parse: %d total rows, %d REGIONSOLUTION rows, %d for region %s", 
                         row_count, regionsolution_count, len(all_rows), region)

            all_rows.sort(key=lambda x: x["timestamp"])
            
            # P5MIN files already contain forward-looking forecasts from when they were generated
            # The first row is the "current" dispatch period, rest are forecasts
            # Return all rows as they're all useful for decision-making
            
            _LOGGER.debug("P5MIN forecast: returning %d periods", len(all_rows))
            
            return all_rows[:periods]

        except Exception as e:
            _LOGGER.error("Error parsing forecasts: %s", e, exc_info=True)
            return []

    def calculate_spike_info(self, current_price: float) -> dict[str, Any]:
        """Calculate spike detection metrics (NO AUTOMATION - just info).
        
        Returns metrics for user to make decisions.
        """
        # Update price history
        self._price_history.append(current_price)
        if len(self._price_history) > 12:  # Keep last hour
            self._price_history = self._price_history[-12:]
        
        if len(self._price_history) < 3:
            return {
                "is_spike": False,
                "spike_magnitude": 0,
                "avg_price": current_price,
                "samples": len(self._price_history),
            }
        
        # Calculate metrics
        avg_price = sum(self._price_history[:-1]) / len(self._price_history[:-1])
        spike_ratio = current_price / avg_price if avg_price != 0 else 1.0
        spike_magnitude = current_price - avg_price
        
        # Detect spike (2x threshold)
        is_spike = spike_ratio > 2.0 and spike_magnitude > 20  # $20/MWh minimum
        
        # Detect negative pricing
        is_negative = current_price < 0
        
        return {
            "is_spike": is_spike,
            "is_negative": is_negative,
            "spike_ratio": round(spike_ratio, 2),
            "spike_magnitude": round(spike_magnitude, 2),
            "current_price": current_price,
            "avg_price": round(avg_price, 2),
            "samples": len(self._price_history),
        }

    async def get_predispatch_forecast_with_file(
        self, region: str, periods: int = 96
    ) -> tuple[list[dict[str, Any]], str]:
        """Fetch predispatch forecast."""
        try:
            async with self._session.get(
                AEMO_PREDISPATCH_BASE_URL,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    return [], ""
                html = await response.text()

            pattern = r'PUBLIC_PREDISPATCH_\d{12}_\d{14}_LEGACY\.zip'
            matches = re.findall(pattern, html)
            
            if not matches:
                return [], ""

            latest_file = sorted(matches)[-1]
            
            if latest_file in self._predispatch_cache:
                cached = self._predispatch_cache[latest_file]
                return cached[:periods], latest_file
            
            file_url = f"{AEMO_PREDISPATCH_BASE_URL}{latest_file}"

            async with self._session.get(
                file_url,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    return [], ""
                content = await response.read()

            forecasts = self._parse_predispatch_zip(content, region)
            self._predispatch_cache = {latest_file: forecasts}
            
            return forecasts[:periods], latest_file

        except Exception as e:
            _LOGGER.error("Error fetching Predispatch: %s", e, exc_info=True)
            return [], ""

    def _parse_predispatch_zip(
        self, content: bytes, region: str
    ) -> list[dict[str, Any]]:
        """Parse Predispatch ZIP."""
        forecasts = []

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for filename in zf.namelist():
                    if filename.upper().endswith('.CSV'):
                        with zf.open(filename) as f:
                            csv_content = f.read().decode("utf-8")
                            reader = csv.reader(io.StringIO(csv_content))
                            
                            for row in reader:
                                if not row or row[0] in ('I', 'C') or len(row) < 9:
                                    continue

                                if row[0] == "D" and row[1] == "PDREGION":
                                    try:
                                        row_region = row[6].strip().strip('"')
                                        if row_region == region:
                                            timestamp = row[7].strip().strip('"')
                                            rrp = float(row[8].strip())

                                            forecasts.append({
                                                "timestamp": timestamp,
                                                "price_mwh": rrp,
                                                "price_cents": rrp / 10,
                                                "price_dollars": rrp / 1000,
                                            })
                                    except (ValueError, IndexError):
                                        continue

            seen = set()
            unique_forecasts = []
            for f in sorted(forecasts, key=lambda x: x["timestamp"]):
                if f["timestamp"] not in seen:
                    seen.add(f["timestamp"])
                    unique_forecasts.append(f)

            # Predispatch files already contain forward-looking forecasts
            # Return all rows as they're all useful for planning
            
            _LOGGER.debug("Predispatch: returning %d periods", len(unique_forecasts))
            
            return unique_forecasts

        except Exception as e:
            _LOGGER.error("Error parsing Predispatch: %s", e, exc_info=True)
            return []