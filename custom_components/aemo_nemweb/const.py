"""Constants for AEMO NEMWEB integration."""

DOMAIN = "aemo_nemweb"

# NEM Regions
NEM_REGIONS = {
    "NSW1": "New South Wales",
    "QLD1": "Queensland",
    "VIC1": "Victoria",
    "SA1": "South Australia",
    "TAS1": "Tasmania",
}

# Region timezone mapping
REGION_TIMEZONES = {
    "NSW1": "Australia/Sydney",
    "QLD1": "Australia/Brisbane",
    "VIC1": "Australia/Melbourne",
    "SA1": "Australia/Adelaide",
    "TAS1": "Australia/Hobart",
}

# Configuration Keys
CONF_NEM_REGION = "nem_region"

# Update Intervals (seconds)
UPDATE_INTERVAL_CURRENT = 300  # 5 minutes for current and 5min forecast
UPDATE_INTERVAL_PREDISPATCH = 1800  # 30 minutes for predispatch forecast

# Sensor Types (only keeping the ones we use)
SENSOR_TYPE_REALTIME_PRICE = "realtime_price"
SENSOR_TYPE_5MIN_FORECAST = "5min_forecast"
SENSOR_TYPE_PREDISPATCH_FORECAST = "predispatch_forecast"

# API URLs (all from NEMWEB)
AEMO_P5MIN_ACTUAL_URL = "https://nemweb.com.au/Reports/Current/P5_Reports/"
AEMO_DISPATCH_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
AEMO_P5MIN_FORECAST_URL = "https://nemweb.com.au/Reports/Current/P5MINFCST/"
AEMO_PREDISPATCH_BASE_URL = "https://nemweb.com.au/Reports/Current/Predispatch_Reports/"