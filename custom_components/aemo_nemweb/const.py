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
SENSOR_TYPE_REALTIME_DEMAND = "realtime_demand"
SENSOR_TYPE_REALTIME_PRICE = "realtime_price"
SENSOR_TYPE_5MIN_FORECAST = "5min_forecast"
SENSOR_TYPE_PREDISPATCH_FORECAST = "predispatch_forecast"

# API URLs (all from NEMWEB)
AEMO_P5MIN_ACTUAL_URL = "https://nemweb.com.au/Reports/Current/P5_Reports/"
AEMO_DISPATCH_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
AEMO_P5MIN_FORECAST_URL = "https://nemweb.com.au/Reports/Current/P5MINFCST/"
AEMO_PREDISPATCH_BASE_URL = "https://nemweb.com.au/Reports/Current/Predispatch_Reports/"

# AEMO Public Dashboard API — lightweight JSON, all regions, ~30 ms response.
# This is the same endpoint used by AEMO's own public dashboard at
# https://dashboards.public.aemo.com.au/dispatch-overview
# The API key below is AEMO's own public key embedded in their dashboard.
AEMO_DASHBOARD_URL = "https://dashboards.public.aemo.com.au/NEM/v1/PWS/NEMDashboard/elecSummary"
AEMO_DASHBOARD_API_KEY = "0ae2748cec08449bb5b3b31b577f71e2"

# How often to poll the dashboard API for realtime price/demand (seconds).
# The underlying data updates every 5 minutes (DISPATCH period), but polling
# every 5 s means we catch a new period within ~5 s of it being published.
DASHBOARD_POLL_INTERVAL = 5