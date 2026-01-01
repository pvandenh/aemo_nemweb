"""Sensor entities for AEMO NEMWEB integration - FIXED NaN display issue."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,  # ADDED: For proper statistics and display
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_NEM_REGION,
    DOMAIN,
    REGION_TIMEZONES,
    SENSOR_TYPE_5MIN_FORECAST,
    SENSOR_TYPE_PREDISPATCH_FORECAST,
    SENSOR_TYPE_REALTIME_PRICE,
)
from .coordinator import AEMOCoordinator

_LOGGER = logging.getLogger(__name__)

# Minimum price threshold - anything below this is treated as zero
# $0.0001/kWh = $0.1/MWh which is effectively free electricity
MIN_PRICE_THRESHOLD = 0.0001


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AEMO NEMWEB sensors from a config entry."""
    coordinator: AEMOCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    region = config_entry.data.get(CONF_NEM_REGION, "NSW1")

    entities = [
        AEMORealtimePriceSensor(coordinator, config_entry, region),
        AEMO5MinForecastSensor(coordinator, config_entry, region),
        AEMOPredispatchForecastSensor(coordinator, config_entry, region),
    ]

    async_add_entities(entities)


class AEMOBaseSensor(CoordinatorEntity[AEMOCoordinator], SensorEntity):
    """Base class for AEMO sensors."""

    _attr_has_entity_name = False  # Use object_id for full entity name

    def __init__(
        self,
        coordinator: AEMOCoordinator,
        config_entry: ConfigEntry,
        region: str,
        sensor_type: str,
        sensor_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._region = region
        self._sensor_type = sensor_type
        
        # Set unique_id and object_id for consistent entity naming
        region_lower = region.lower()
        self._attr_unique_id = f"aemo_nemweb_{region_lower}_{sensor_type}"
        self._attr_name = sensor_name
        
        # Use object_id to control entity_id format
        # This will create entity_id like: sensor.aemo_nemweb_nsw1_realtime_price
        object_id = f"aemo_nemweb_{region_lower}_{sensor_type}"
        self.entity_id = f"sensor.{object_id}"
        
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": f"AEMO NEMWEB ({region})",
            "manufacturer": "AEMO",
            "model": "NEM Wholesale Prices",
        }

    def _convert_to_iso_timestamp(self, timestamp: str) -> str:
        """Convert AEMO timestamp to ISO with timezone.
        
        IMPORTANT: AEMO data is ALWAYS in AEST (UTC+10), not local Sydney time.
        During daylight saving, Sydney is AEDT (UTC+11), but AEMO still uses AEST.
        """
        if not timestamp or "/" not in timestamp:
            return timestamp

        try:
            from datetime import timezone, timedelta
            
            # AEMO always uses AEST (UTC+10), regardless of daylight saving
            aest = timezone(timedelta(hours=10))
            
            # Parse the timestamp
            dt = datetime.strptime(timestamp, "%Y/%m/%d %H:%M:%S")
            
            # Apply AEST timezone (always UTC+10)
            dt = dt.replace(tzinfo=aest)
            
            return dt.isoformat()
        except (ValueError, TypeError):
            return timestamp

    def _normalize_price(self, price: float | None) -> float | None:
        """Normalize price value to prevent NaN display.
        
        Very small prices (< $0.0001/kWh) are treated as zero to avoid
        scientific notation display issues in Home Assistant UI.
        
        Args:
            price: Raw price value in $/kWh
            
        Returns:
            Normalized price or None if invalid
        """
        if price is None:
            return None
            
        try:
            price_float = float(price)
            # Treat extremely small values as zero
            # Anything less than $0.0001/kWh ($0.1/MWh) is effectively free
            if abs(price_float) < MIN_PRICE_THRESHOLD:
                return 0.0
            # Round to 4 decimal places to avoid scientific notation
            return round(price_float, 4)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid price value: %s", price)
            return None


class AEMORealtimePriceSensor(AEMOBaseSensor):
    """Sensor for real-time price from DISPATCH files (fastest updates)."""

    _attr_native_unit_of_measurement = "$/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT  # FIXED: Added for proper statistics
    _attr_icon = "mdi:lightning-bolt"
    _attr_suggested_display_precision = 4  # FIXED: Show 4 decimal places

    def __init__(
        self,
        coordinator: AEMOCoordinator,
        config_entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, 
            config_entry, 
            region, 
            SENSOR_TYPE_REALTIME_PRICE,
            "AEMO NEMWEB Realtime Price"
        )

    @property
    def native_value(self) -> float | None:
        """Return the current real-time price in $/kWh.
        
        FIXED: Normalizes tiny values to prevent NaN/scientific notation display.
        """
        if not self.coordinator.data:
            return None

        # Try DISPATCH data first (fastest updates)
        realtime_data = self.coordinator.data.get("realtime_price")
        if realtime_data:
            price = realtime_data.get("price_dollars")
            return self._normalize_price(price)
        
        # Fallback to spot price if DISPATCH not available
        spot_data = self.coordinator.data.get("spot_price")
        if spot_data:
            price = spot_data.get("price_dollars")
            return self._normalize_price(price)
        
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return {}

        realtime_data = self.coordinator.data.get("realtime_price")
        if not realtime_data:
            # Fallback to spot price
            realtime_data = self.coordinator.data.get("spot_price")
        
        if not realtime_data:
            return {"region": self._region, "source": "waiting for data"}

        timestamp = realtime_data.get("timestamp", "")
        
        return {
            "price_mwh": realtime_data.get("price_mwh"),
            "price_cents": realtime_data.get("price_cents"),
            "timestamp": self._convert_to_iso_timestamp(timestamp),
            "region": self._region,
            "source": "DISPATCH" if self.coordinator._dispatch_available else "P5MIN",
        }


class AEMO5MinForecastSensor(AEMOBaseSensor):
    """Sensor for 5-minute price forecast."""

    _attr_native_unit_of_measurement = "$/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT  # FIXED: Added
    _attr_icon = "mdi:chart-line"
    _attr_suggested_display_precision = 4  # FIXED: Added

    def __init__(
        self,
        coordinator: AEMOCoordinator,
        config_entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            config_entry,
            region,
            SENSOR_TYPE_5MIN_FORECAST,
            "AEMO NEMWEB 5 Minute Forecast"
        )

    @property
    def native_value(self) -> float | None:
        """Return next period forecast price."""
        if self.coordinator.data:
            forecast = self.coordinator.data.get("p5min_forecast", [])
            if forecast and len(forecast) > 0:
                price = forecast[0].get("price_dollars")
                return self._normalize_price(price)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast data for EMHASS."""
        attrs = {
            "region": self._region,
            "unit": "$/kWh",
            "forecast": [],
            "timestamps": [],
            "forecast_dict": {},
            "forecast_cents": [],
            "forecast_mwh": [],
            "forecast_length": 0,
        }

        if self.coordinator.data and self.coordinator.data.get("p5min_forecast"):
            forecast = self.coordinator.data["p5min_forecast"]

            prices = []
            timestamps = []
            forecast_dict = {}
            prices_cents = []
            prices_mwh = []

            for period in forecast:
                price = period.get("price_dollars", 0)
                raw_ts = period.get("timestamp", "")
                iso_ts = self._convert_to_iso_timestamp(raw_ts)

                # FIXED: Normalize price to prevent scientific notation
                normalized_price = self._normalize_price(price)
                if normalized_price is None:
                    normalized_price = 0.0

                prices.append(normalized_price)
                timestamps.append(iso_ts)
                prices_cents.append(period.get("price_cents", 0))
                prices_mwh.append(period.get("price_mwh", 0))

                if iso_ts:
                    forecast_dict[iso_ts] = normalized_price

            attrs["forecast"] = prices
            attrs["timestamps"] = timestamps
            attrs["forecast_dict"] = forecast_dict
            attrs["forecast_cents"] = prices_cents
            attrs["forecast_mwh"] = prices_mwh
            attrs["forecast_length"] = len(prices)

        if self.coordinator.data:
            attrs["last_update"] = self.coordinator.data.get("last_update")

        return attrs


class AEMOPredispatchForecastSensor(AEMOBaseSensor):
    """Sensor for 30-minute Predispatch forecast."""

    _attr_native_unit_of_measurement = "$/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT  # FIXED: Added
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_suggested_display_precision = 4  # FIXED: Added

    def __init__(
        self,
        coordinator: AEMOCoordinator,
        config_entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            config_entry,
            region,
            SENSOR_TYPE_PREDISPATCH_FORECAST,
            "AEMO NEMWEB Predispatch Forecast"
        )

    @property
    def native_value(self) -> float | None:
        """Return next period forecast price."""
        if self.coordinator.data:
            forecast = self.coordinator.data.get("predispatch_forecast", [])
            if forecast and len(forecast) > 0:
                price = forecast[0].get("price_dollars")
                return self._normalize_price(price)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast data for EMHASS."""
        attrs = {
            "region": self._region,
            "unit": "$/kWh",
            "forecast": [],
            "timestamps": [],
            "forecast_dict": {},
            "forecast_cents": [],
            "forecast_mwh": [],
            "forecast_length": 0,
        }

        if self.coordinator.data and self.coordinator.data.get("predispatch_forecast"):
            forecast = self.coordinator.data["predispatch_forecast"]

            prices = []
            timestamps = []
            forecast_dict = {}
            prices_cents = []
            prices_mwh = []

            for period in forecast:
                price = period.get("price_dollars", 0)
                raw_ts = period.get("timestamp", "")
                iso_ts = self._convert_to_iso_timestamp(raw_ts)

                # FIXED: Normalize price to prevent scientific notation
                normalized_price = self._normalize_price(price)
                if normalized_price is None:
                    normalized_price = 0.0

                prices.append(normalized_price)
                timestamps.append(iso_ts)
                prices_cents.append(period.get("price_cents", 0))
                prices_mwh.append(period.get("price_mwh", 0))

                if iso_ts:
                    forecast_dict[iso_ts] = normalized_price

            attrs["forecast"] = prices
            attrs["timestamps"] = timestamps
            attrs["forecast_dict"] = forecast_dict
            attrs["forecast_cents"] = prices_cents
            attrs["forecast_mwh"] = prices_mwh
            attrs["forecast_length"] = len(prices)

        if self.coordinator.data:
            attrs["last_update"] = self.coordinator.data.get("last_update")

        return attrs