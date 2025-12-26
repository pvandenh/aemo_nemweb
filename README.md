# AEMO NEMWEB Quick Reference

## Installation
```
HACS → Integrations → ⋮ → Custom Repositories
URL: https://github.com/pedrov/aemo_nemweb
Category: Integration
```

## Configuration
```
Settings → Devices & Services → + Add Integration → AEMO NEMWEB
Select Region: NSW1 / QLD1 / VIC1 / SA1 / TAS1
```

## Sensors

| Sensor | Updates | Description |
|--------|---------|-------------|
| `sensor.aemo_nemweb_{region}_realtime_price` | 5s | Current spot price |
| `sensor.aemo_nemweb_{region}_5min_forecast` | 30s | Next hour forecast |
| `sensor.aemo_nemweb_{region}_predispatch_forecast` | 5m | 48-hour forecast |

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
