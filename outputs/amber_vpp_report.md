# Amber / VPP Battery Simulator Report

This is a simulated engineering model, not a tariff quote.

Volatility profile: `conservative`

## Best Simulated Case

- Season: `summer`
- Mode: `amber_smartshift`
- Battery: `40 kWh`
- Estimated value: `329.13 AUD / 30 days`

## Full Comparison

| season | volatility | mode | capacity_kwh | baseline_cost_per_day | optimized_cost_per_day | savings_per_day | savings_per_month_30d | savings_per_year_365d | throughput_kwh |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| summer | conservative | amber_smartshift | 40.00 | 9.22 | -1.75 | 10.97 | 329.13 | 4004.40 | 78.08 |
| summer | conservative | amber_smartshift | 20.00 | 9.22 | 1.56 | 7.66 | 229.91 | 2797.18 | 52.07 |
| summer | conservative | fixed_vpp | 40.00 | 5.21 | -0.74 | 5.96 | 178.70 | 2174.19 | 45.38 |
| summer | conservative | self_ha | 40.00 | 5.21 | -0.09 | 5.30 | 158.97 | 1934.19 | 45.38 |
| summer | conservative | fixed_vpp | 20.00 | 5.21 | 0.40 | 4.81 | 144.32 | 1755.86 | 34.42 |
| summer | conservative | amber_smartshift | 10.00 | 9.22 | 4.59 | 4.63 | 138.83 | 1689.14 | 26.03 |
| winter | conservative | amber_smartshift | 40.00 | 13.23 | 8.72 | 4.51 | 135.28 | 1645.92 | 55.89 |
| winter | conservative | fixed_vpp | 40.00 | 10.42 | 6.07 | 4.35 | 130.57 | 1588.61 | 49.98 |
| winter | conservative | amber_smartshift | 20.00 | 13.23 | 9.01 | 4.21 | 126.42 | 1538.10 | 51.88 |
| summer | conservative | self_ha | 20.00 | 5.21 | 1.06 | 4.15 | 124.59 | 1515.86 | 34.42 |
| winter | conservative | fixed_vpp | 20.00 | 10.42 | 6.48 | 3.94 | 118.10 | 1436.87 | 42.63 |
| winter | conservative | self_ha | 40.00 | 10.42 | 6.72 | 3.69 | 110.84 | 1348.61 | 49.98 |
| winter | conservative | amber_smartshift | 10.00 | 13.23 | 9.77 | 3.46 | 103.66 | 1261.20 | 33.86 |
| winter | conservative | self_ha | 20.00 | 10.42 | 7.14 | 3.28 | 98.37 | 1196.87 | 42.63 |
| summer | conservative | fixed_vpp | 10.00 | 5.21 | 1.94 | 3.27 | 98.11 | 1193.62 | 26.03 |
| winter | conservative | fixed_vpp | 10.00 | 10.42 | 7.34 | 3.08 | 92.31 | 1123.09 | 27.95 |
| summer | conservative | self_ha | 10.00 | 5.21 | 2.60 | 2.61 | 78.38 | 953.62 | 26.03 |
| winter | conservative | self_ha | 10.00 | 10.42 | 8.00 | 2.42 | 72.58 | 883.09 | 27.95 |

## How To Read This

- `self_ha` is the Home Assistant / local-control path. It mainly wins by
  avoiding retail imports and shifting PV into evening load.
- `amber_smartshift` is the wholesale-exposed path. It is highly sensitive to
  volatility. Strong summer price spikes help; flat winter spreads hurt.
- `fixed_vpp` is the traditional VPP path. It has lower upside, but the fixed
  credit makes the result steadier.

## Engineering Takeaway

Small batteries do not need to beat grid-scale BESS in pure wholesale trading
to be useful. Their strongest defensible value is usually behind-the-meter:
PV self-consumption, retail bill avoidance, demand shaping, and selective export
during high-value events.

The next serious upgrade is to replace the simulated profiles with real data:
AEMO/NEM price traces, Amber import/export prices, smart meter load, PV inverter
telemetry, and battery SOC history.
