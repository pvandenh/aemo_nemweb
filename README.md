# AEMO NEMWEB Quick Reference

This integration provides basic yet reliable AEMO spot and forecast data values.
It uses NEMWEB as data source, with a focus being on speed of data fetch.
It checks for new data each minute, and upon the commencement of the 5th minute it will speed up to checking for updating every second, until new data is found (at which point it will slow down polling again to each minute).
Average time on new data being present is <30 seconds into the interval.

## Installation
```
HACS → Integrations → ⋮ → Custom Repositories
URL: https://github.com/pvandenh/aemo_nemweb
Category: Integration
Search and add AEMO NEMWEB
Restart Home Assistant

```

## Configuration
```
Settings → Devices & Services → + Add Integration → AEMO NEMWEB
Select Region: NSW1 / QLD1 / VIC1 / SA1 / TAS1
```

## Sensors

| Sensor | Updates | Description |
|--------|---------|-------------|
| `sensor.aemo_nemweb_{region}_realtime_price` | 1s | Current spot price |
| `sensor.aemo_nemweb_{region}_5min_forecast` | 30s | Next hour forecast |
| `sensor.aemo_nemweb_{region}_predispatch_forecast` | 5m | 48-hour forecast |
| `sensor.aemo_nemweb_{region}_realtime_demand` | 5m | Current cleared demand |

## Useful Attributes

Access forecast data example:
```yaml
{{ state_attr('sensor.aemo_nemweb_nsw1_5min_forecast', 'forecast') }}
{{ state_attr('sensor.aemo_nemweb_nsw1_5min_forecast', 'timestamps') }}
{{ state_attr('sensor.aemo_nemweb_nsw1_5min_forecast', 'forecast_dict') }}
```

## Data Source
Official AEMO NEMWEB: https://nemweb.com.au/
All timestamps in AEST (UTC+10)
