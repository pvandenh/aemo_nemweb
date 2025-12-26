# AEMO NEMWEB Quick Reference

## Installation
```
HACS → Integrations → ⋮ → Custom Repositories
URL: https://github.com/pedrov/aemo_nemweb
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
## 12 hour forecast graph example

```
type: custom:apexcharts-card
header:
  show: true
  show_states: true
  colorize_states: true
  title: AEMO Price Forecast
graph_span: 12h
span:
  start: minute
update_interval: 30s
apex_config:
  chart:
    height: 400px
  legend:
    show: true
    position: top
  fill:
    type: solid
    opacity: 1
  yaxis:
    - id: price
      decimals: 4
      forceNiceScale: true
      labels:
        formatter: |
          EVAL:function(value) {
            return '$' + value.toFixed(4) + '/kWh';
          }
  tooltip:
    x:
      format: ddd HH:mm
    "y":
      formatter: |
        EVAL:function(value) {
          return '$' + value.toFixed(4) + '/kWh';
        }
  xaxis:
    labels:
      format: HH:mm
      datetimeUTC: false
  stroke:
    width:
      - 3
      - 2
      - 2
    curve: smooth
  annotations:
    yaxis:
      - "y": 0
        borderColor: "#00CC00"
        strokeDashArray: 3
        label:
          text: $0
          style:
            color: "#fff"
            background: "#00CC00"
      - "y": 0.1
        borderColor: "#FFA500"
        strokeDashArray: 3
        opacity: 0.3
        label:
          text: $0.10
          style:
            color: "#666"
            background: "#FFF"
series:
  - entity: sensor.aemo_nemweb_nsw1_realtime_price
    name: Current
    type: line
    stroke_width: 0
    color: "#87CEEB"
    show:
      in_header: true
      legend_value: true
      in_chart: false
    float_precision: 4
  - entity: sensor.aemo_peak_forecast
    name: Peak
    float_precision: 3
    color: "#FF0000"
    show:
      in_header: true
      legend_value: true
      in_chart: false
  - entity: sensor.aemo_nemweb_nsw1_5min_forecast
    name: 5-Min Forecast
    type: line
    stroke_width: 2
    color: "#4BC0C0"
    curve: smooth
    fill_raw: last
    extend_to: false
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const forecast = entity.attributes.forecast || [];
      const timestamps = entity.attributes.timestamps || [];
      if (forecast.length === 0 || timestamps.length === 0) {
        return [];
      }
      const now = new Date().getTime();
      const points = [];
      for (let i = 0; i < forecast.length && i < timestamps.length; i++) {
        const time = new Date(timestamps[i]).getTime();
        if (time > now) {
          points.push([time, forecast[i]]);
        }
      }
      return points;
  - entity: sensor.aemo_nemweb_nsw1_predispatch_forecast
    name: Predispatch Forecast
    type: line
    stroke_width: 2
    color: "#9966FF"
    curve: smooth
    fill_raw: last
    extend_to: false
    show:
      in_header: false
      legend_value: false
    data_generator: >
      const forecast = entity.attributes.forecast || [];

      const timestamps = entity.attributes.timestamps || [];

      if (forecast.length === 0 || timestamps.length === 0) {
        return [];
      }


      const now = new Date().getTime();


      // Get the last 5-min forecast timestamp to avoid overlap

      const fiveMinEntity =
      hass.states['sensor.aemo_nemweb_nsw1_5min_forecast'];

      const fiveMinTimestamps = fiveMinEntity?.attributes?.timestamps || [];


      let cutoffTime = now;


      // If we have 5-min forecasts, start predispatch AFTER the last one

      if (fiveMinTimestamps.length > 0) {
        const fiveMinTimes = fiveMinTimestamps
          .map(ts => new Date(ts).getTime())
          .filter(t => t > now);
        
        if (fiveMinTimes.length > 0) {
          // Start predispatch after the last 5-min forecast point
          cutoffTime = Math.max(...fiveMinTimes);
        }
      }


      const points = [];

      for (let i = 0; i < forecast.length && i < timestamps.length; i++) {
        const time = new Date(timestamps[i]).getTime();
        
        // Only include points after the 5-min forecast ends
        if (time > cutoffTime) {
          points.push([time, forecast[i]]);
        }
      }


      return points;

```
## Data Source
Official AEMO NEMWEB: https://nemweb.com.au/
All timestamps in AEST (UTC+10)
