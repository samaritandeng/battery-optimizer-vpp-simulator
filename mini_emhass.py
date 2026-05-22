"""
Mini-EMHASS: household energy scheduler
=======================================

This is a learning-friendly version of the core idea behind EMHASS:

    forecasts + device constraints + electricity prices
        -> linear optimization
        -> optimal home energy schedule

What is included:
    - PV forecast
    - household base load forecast
    - import/export electricity prices
    - battery charge/discharge scheduling
    - one flexible / deferrable load, such as EV charging or water heating
    - grid import/export and optional PV curtailment
    - CSV, chart, and Home Assistant style action JSON outputs

It is intentionally much smaller than EMHASS. The goal is to make the
optimization model readable enough that you can extend it yourself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyomo.environ as pyo


HOURS = 24
DT = 1.0


def load_config(path: Path) -> Dict[str, Any]:
    """Load JSON config from disk.

    Why JSON?
    ---------
    For a small learning project, JSON is enough and is built into Python.
    Real EMS/VPP systems often move this to YAML, a database, or Home Assistant
    entities. The important idea is the same: keep tunable parameters outside
    the optimization code.
    """
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_vector(name: str, values: np.ndarray) -> None:
    """Make sure a forecast vector has exactly 24 hourly values."""
    if len(values) != HOURS:
        raise ValueError(f"{name} must contain {HOURS} values, got {len(values)}")


def make_forecasts(config: Dict[str, Any]) -> pd.DataFrame:
    """Create a 24-hour forecast table.

    EMHASS can pull data from Home Assistant, Solcast, Forecast.Solar, PVLib,
    Nord Pool, Amber, and user-provided runtime parameters. This small tool
    starts with deterministic simulated profiles so the optimization model is
    easy to inspect before connecting real APIs.
    """
    hours = np.arange(HOURS)
    sim = config["simulation"]

    # Household base load: low overnight, breakfast bump, evening peak.
    base_load = np.array(
        [
            0.7, 0.6, 0.6, 0.6, 0.7, 0.9,
            1.4, 1.8, 1.5, 1.1, 0.9, 0.8,
            0.8, 0.9, 1.0, 1.1, 1.4, 2.0,
            2.8, 3.0, 2.6, 2.0, 1.3, 0.9,
        ],
        dtype=float,
    )

    # PV forecast: a simple bell-shaped curve between sunrise and sunset.
    sunrise = sim.get("sunrise_hour", 6)
    sunset = sim.get("sunset_hour", 18)
    pv_peak = sim.get("pv_peak_kw", 5.0)
    daylight_fraction = (hours - sunrise) / max(sunset - sunrise, 1)
    pv = pv_peak * np.sin(np.pi * daylight_fraction)
    pv = np.clip(pv, 0.0, None)

    # Import price: cheap overnight, expensive evening peak.
    import_price = np.full(HOURS, 0.45)
    import_price[0:6] = 0.22
    import_price[6:10] = 0.55
    import_price[17:22] = 0.90
    import_price[22:24] = 0.50

    # Export price: usually lower than import price, with a small evening bonus.
    export_price = np.full(HOURS, 0.08)
    export_price[17:21] = 0.25

    # The config may override any profile with a 24-value array.
    if "base_load_kw" in sim:
        base_load = np.array(sim["base_load_kw"], dtype=float)
    if "pv_kw" in sim:
        pv = np.array(sim["pv_kw"], dtype=float)
    if "import_price" in sim:
        import_price = np.array(sim["import_price"], dtype=float)
    if "export_price" in sim:
        export_price = np.array(sim["export_price"], dtype=float)

    for name, values in {
        "base_load_kw": base_load,
        "pv_kw": pv,
        "import_price": import_price,
        "export_price": export_price,
    }.items():
        validate_vector(name, values)

    return pd.DataFrame(
        {
            "hour": hours,
            "base_load_kw": base_load,
            "pv_kw": pv,
            "import_price": import_price,
            "export_price": export_price,
        }
    )


def build_model(
    forecast: pd.DataFrame,
    config: Dict[str, Any],
    cost_function: str,
) -> pyo.ConcreteModel:
    """Build the Pyomo optimization model.

    The key equation is the household power balance:

        grid_import + pv + battery_discharge
            = base_load + deferrable_load + battery_charge
              + grid_export + pv_curtailment

    This is the EMS equivalent of Kirchhoff's current law for a single home
    energy bus: every kW entering the bus must leave through some sink.
    """
    battery = config["battery"]
    grid = config["grid"]
    flex = config["deferrable_load"]

    m = pyo.ConcreteModel("mini_emhass")
    m.T = pyo.RangeSet(0, HOURS - 1)
    m.T_soc = pyo.RangeSet(0, HOURS)

    # Convert pandas columns to Pyomo Params.
    # Pyomo Params are indexed symbolic constants used inside algebraic rules.
    m.base_load = pyo.Param(m.T, initialize=forecast["base_load_kw"].to_dict())
    m.pv = pyo.Param(m.T, initialize=forecast["pv_kw"].to_dict())
    m.import_price = pyo.Param(m.T, initialize=forecast["import_price"].to_dict())
    m.export_price = pyo.Param(m.T, initialize=forecast["export_price"].to_dict())

    # Grid variables. Import and export are split into two nonnegative variables
    # because LP solvers handle nonnegative variables very efficiently.
    m.grid_import = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0, grid["import_limit_kw"]),
    )
    m.grid_export = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0, grid["export_limit_kw"]),
    )

    # Battery variables.
    m.p_batt_ch = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0, battery["p_charge_max_kw"]),
    )
    m.p_batt_dis = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0, battery["p_discharge_max_kw"]),
    )
    m.soc = pyo.Var(
        m.T_soc,
        domain=pyo.NonNegativeReals,
        bounds=(battery["soc_min_kwh"], battery["soc_max_kwh"]),
    )

    # PV curtailment is allowed so the model remains feasible even when PV
    # production exceeds load + battery + export limit.
    m.pv_curtail = pyo.Var(m.T, domain=pyo.NonNegativeReals)

    # Flexible load power. For an EV charger or heat pump, continuous power is
    # a reasonable first approximation. A washing machine with fixed cycles
    # would need binary variables and a MILP model.
    max_flex_kw = flex["max_power_kw"] if flex["enabled"] else 0.0
    m.p_flex = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0, max_flex_kw))

    # Initial and optional cyclic final SOC.
    m.c_soc_init = pyo.Constraint(expr=m.soc[0] == battery["soc_init_kwh"])
    if battery.get("cyclic_soc", True):
        m.c_soc_final = pyo.Constraint(expr=m.soc[HOURS] == battery["soc_init_kwh"])

    def _battery_balance(m, t):
        return m.soc[t + 1] == (
            m.soc[t]
            + m.p_batt_ch[t] * battery["charge_efficiency"] * DT
            - m.p_batt_dis[t] / battery["discharge_efficiency"] * DT
        )

    m.c_battery_balance = pyo.Constraint(m.T, rule=_battery_balance)

    def _home_power_balance(m, t):
        return (
            m.grid_import[t]
            + m.pv[t]
            + m.p_batt_dis[t]
            == m.base_load[t]
            + m.p_flex[t]
            + m.p_batt_ch[t]
            + m.grid_export[t]
            + m.pv_curtail[t]
        )

    m.c_home_power_balance = pyo.Constraint(m.T, rule=_home_power_balance)

    # Deferrable load window. We use [earliest_hour, latest_hour), meaning the
    # start is included and the end is excluded, matching Python slicing.
    earliest = flex["earliest_hour"]
    latest = flex["latest_hour"]

    def _flex_window(m, t):
        if flex["enabled"] and earliest <= t < latest:
            return pyo.Constraint.Skip
        return m.p_flex[t] == 0

    m.c_flex_window = pyo.Constraint(m.T, rule=_flex_window)

    if flex["enabled"]:
        window_hours = max(latest - earliest, 0)
        max_possible_energy = window_hours * flex["max_power_kw"] * DT
        if flex["energy_required_kwh"] > max_possible_energy + 1e-9:
            raise ValueError(
                "Deferrable load is infeasible: required energy is larger than "
                "max_power_kw * available window hours."
            )

        m.c_flex_energy = pyo.Constraint(
            expr=sum(m.p_flex[t] * DT for t in m.T) == flex["energy_required_kwh"]
        )

    def _operating_cost(m):
        energy_bill = sum(
            (
                m.import_price[t] * m.grid_import[t]
                - m.export_price[t] * m.grid_export[t]
            )
            * DT
            for t in m.T
        )
        battery_wear = sum(
            battery["degradation_cost_per_kwh"]
            * (m.p_batt_ch[t] + m.p_batt_dis[t])
            * DT
            for t in m.T
        )
        return energy_bill + battery_wear

    if cost_function in {"cost", "profit"}:
        # "cost" minimizes net electricity bill.
        # "profit" maximizes negative cost; same optimum, clearer name for VPP.
        sense = pyo.minimize if cost_function == "cost" else pyo.maximize
        expr = _operating_cost(m) if cost_function == "cost" else -_operating_cost(m)
        m.obj = pyo.Objective(expr=expr, sense=sense)
    elif cost_function == "self-consumption":
        # Self-consumption mode penalizes grid exchange and curtailment first,
        # then uses bill cost as a tiny tie-breaker.
        grid_exchange = sum(
            (m.grid_import[t] + m.grid_export[t] + m.pv_curtail[t]) * DT
            for t in m.T
        )
        m.obj = pyo.Objective(
            expr=grid_exchange + 0.001 * _operating_cost(m),
            sense=pyo.minimize,
        )
    else:
        raise ValueError(f"Unsupported cost function: {cost_function}")

    return m


def solve_model(model: pyo.ConcreteModel) -> None:
    """Solve the model using HiGHS through Pyomo's APPSI interface."""
    solver = pyo.SolverFactory("appsi_highs")
    result = solver.solve(model)
    status = result.solver.termination_condition
    print(f"Solve status: {status}")
    if str(status).lower() != "optimal":
        raise RuntimeError(f"Optimization failed with status: {status}")


def value(x: Any) -> float:
    """Return a clean float from a Pyomo expression and remove tiny noise."""
    v = float(pyo.value(x))
    return 0.0 if abs(v) < 1e-7 else v


def extract_results(
    model: pyo.ConcreteModel,
    forecast: pd.DataFrame,
    config: Dict[str, Any],
) -> pd.DataFrame:
    """Convert the solved Pyomo model into a pandas table."""
    battery = config["battery"]

    rows = []
    for t in range(HOURS):
        p_ch = value(model.p_batt_ch[t])
        p_dis = value(model.p_batt_dis[t])
        grid_import = value(model.grid_import[t])
        grid_export = value(model.grid_export[t])
        p_flex = value(model.p_flex[t])
        pv_curtail = value(model.pv_curtail[t])

        bill_cost = (
            forecast.loc[t, "import_price"] * grid_import
            - forecast.loc[t, "export_price"] * grid_export
        ) * DT
        battery_wear = battery["degradation_cost_per_kwh"] * (p_ch + p_dis) * DT

        if p_ch > 1e-5:
            mode = "charge"
        elif p_dis > 1e-5:
            mode = "discharge"
        else:
            mode = "idle"

        rows.append(
            {
                "hour": t,
                "base_load_kw": forecast.loc[t, "base_load_kw"],
                "pv_kw": forecast.loc[t, "pv_kw"],
                "import_price": forecast.loc[t, "import_price"],
                "export_price": forecast.loc[t, "export_price"],
                "flex_load_kw": p_flex,
                "battery_mode": mode,
                "battery_charge_kw": p_ch,
                "battery_discharge_kw": p_dis,
                "battery_setpoint_kw": p_ch - p_dis,
                "soc_start_kwh": value(model.soc[t]),
                "soc_end_kwh": value(model.soc[t + 1]),
                "grid_import_kw": grid_import,
                "grid_export_kw": grid_export,
                "net_grid_kw": grid_import - grid_export,
                "pv_curtail_kw": pv_curtail,
                "bill_cost": bill_cost,
                "battery_wear_cost": battery_wear,
                "total_cost": bill_cost + battery_wear,
            }
        )

    df = pd.DataFrame(rows)
    df["baseline_cost"] = compute_baseline_cost(forecast, config)
    df["cumulative_cost"] = df["total_cost"].cumsum()
    df["cumulative_baseline_cost"] = df["baseline_cost"].cumsum()
    df["cumulative_savings"] = df["cumulative_baseline_cost"] - df["cumulative_cost"]
    df["cumulative_profit"] = -df["cumulative_cost"]
    return df


def compute_baseline_cost(forecast: pd.DataFrame, config: Dict[str, Any]) -> np.ndarray:
    """Compute a simple no-optimizer baseline bill.

    Baseline assumption:
        - no battery dispatch
        - PV first serves household load
        - remaining PV exports up to the grid export limit
        - the flexible load is spread evenly across its allowed window

    This gives us a "savings versus doing nothing smart" number, which is much
    easier to interpret than raw optimized bill cost.
    """
    flex = config["deferrable_load"]
    grid = config["grid"]
    p_flex = np.zeros(HOURS)

    if flex["enabled"]:
        earliest = flex["earliest_hour"]
        latest = flex["latest_hour"]
        window = max(latest - earliest, 1)
        even_power = min(flex["energy_required_kwh"] / (window * DT), flex["max_power_kw"])
        p_flex[earliest:latest] = even_power

    net_load = forecast["base_load_kw"].to_numpy() + p_flex - forecast["pv_kw"].to_numpy()
    grid_import = np.clip(net_load, 0.0, grid["import_limit_kw"])
    grid_export = np.clip(-net_load, 0.0, grid["export_limit_kw"])

    return (
        forecast["import_price"].to_numpy() * grid_import
        - forecast["export_price"].to_numpy() * grid_export
    ) * DT


def write_home_assistant_action(
    df: pd.DataFrame,
    config: Dict[str, Any],
    current_hour: int,
    output_path: Path,
) -> None:
    """Write one simple JSON action file for Home Assistant style automation.

    This does not call Home Assistant directly. It creates the payload you would
    normally publish to MQTT, REST, or HA sensors in a real integration.
    """
    if not 0 <= current_hour < HOURS:
        raise ValueError("current_hour must be between 0 and 23")

    row = df.iloc[current_hour]
    action = {
        "tool": "mini_emhass",
        "current_hour": int(current_hour),
        "battery_mode": row["battery_mode"],
        "battery_setpoint_kw": round(float(row["battery_setpoint_kw"]), 3),
        "setpoint_convention": "positive=charge, negative=discharge",
        "flexible_load_name": config["deferrable_load"]["name"],
        "flexible_load_setpoint_kw": round(float(row["flex_load_kw"]), 3),
        "expected_grid_import_kw": round(float(row["grid_import_kw"]), 3),
        "expected_grid_export_kw": round(float(row["grid_export_kw"]), 3),
        "soc_target_end_kwh": round(float(row["soc_end_kwh"]), 3),
    }
    output_path.write_text(json.dumps(action, indent=2), encoding="utf-8")
    print(f"Home Assistant style action saved: {output_path}")


def plot_results(df: pd.DataFrame, output_path: Path) -> None:
    """Create a multi-panel chart for price, forecast, battery, and grid plan."""
    fig, axes = plt.subplots(4, 1, figsize=(12, 12), sharex=True)

    ax = axes[0]
    ax.step(df["hour"], df["import_price"], where="post", label="Import price", linewidth=2)
    ax.step(df["hour"], df["export_price"], where="post", label="Export price", linewidth=2)
    ax.set_ylabel("Price")
    ax.set_title("Electricity Price Forecast")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    ax = axes[1]
    ax.plot(df["hour"], df["base_load_kw"], label="Base load", linewidth=2)
    ax.plot(df["hour"], df["pv_kw"], label="PV forecast", linewidth=2)
    ax.bar(df["hour"], df["flex_load_kw"], alpha=0.45, label="Flexible load")
    ax.set_ylabel("kW")
    ax.set_title("Load, PV, and Deferrable Load")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    ax = axes[2]
    ax.bar(df["hour"], df["battery_charge_kw"], label="Battery charge", alpha=0.75)
    ax.bar(df["hour"], -df["battery_discharge_kw"], label="Battery discharge", alpha=0.75)
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_ylabel("Battery kW")
    ax.set_title("Battery Dispatch and SOC")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    ax_soc = ax.twinx()
    soc_x = list(df["hour"]) + [HOURS]
    soc_y = [df["soc_start_kwh"].iloc[0]] + list(df["soc_end_kwh"])
    ax_soc.plot(soc_x, soc_y, color="#2a9d8f", marker="o", linewidth=2, label="SOC")
    ax_soc.set_ylabel("SOC kWh", color="#2a9d8f")
    ax_soc.tick_params(axis="y", labelcolor="#2a9d8f")
    ax_soc.legend(loc="upper right")

    ax = axes[3]
    ax.bar(df["hour"], df["grid_import_kw"], label="Grid import", alpha=0.75)
    ax.bar(df["hour"], -df["grid_export_kw"], label="Grid export", alpha=0.75)
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_ylabel("Grid kW")
    ax.set_xlabel("Hour")
    ax.set_title(
        f"Grid Plan and Cumulative Savings (total: {df['cumulative_savings'].iloc[-1]:.2f})"
    )
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    ax_profit = ax.twinx()
    ax_profit.plot(
        df["hour"],
        df["cumulative_savings"],
        color="#e76f51",
        marker="o",
        linewidth=2,
        label="Cumulative savings",
    )
    ax_profit.set_ylabel("Savings")
    ax_profit.legend(loc="upper right")

    axes[-1].set_xticks(range(0, 25, 2))
    fig.tight_layout()
    fig.savefig(output_path, dpi=140)
    print(f"Chart saved: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mini-EMHASS: household energy optimization with Pyomo"
    )
    parser.add_argument(
        "--config",
        default="config_mini_emhass.json",
        help="Path to JSON config file",
    )
    parser.add_argument(
        "--cost-function",
        choices=["cost", "profit", "self-consumption"],
        default=None,
        help="Optimization objective. Defaults to config value.",
    )
    parser.add_argument(
        "--current-hour",
        type=int,
        default=0,
        help="Hour used for the Home Assistant style action JSON",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    cost_function = args.cost_function or config.get("cost_function", "cost")

    forecast = make_forecasts(config)
    model = build_model(forecast, config, cost_function)
    solve_model(model)
    df = extract_results(model, forecast, config)

    output = config["output"]
    schedule_path = Path(output["schedule_csv"])
    plot_path = Path(output["plot_png"])
    action_path = Path(output["actions_json"])

    df.to_csv(schedule_path, index=False)
    print(f"Schedule saved: {schedule_path}")

    write_home_assistant_action(df, config, args.current_hour, action_path)
    plot_results(df, plot_path)

    show_cols = [
        "hour",
        "base_load_kw",
        "pv_kw",
        "import_price",
        "flex_load_kw",
        "battery_mode",
        "battery_setpoint_kw",
        "soc_end_kwh",
        "grid_import_kw",
        "grid_export_kw",
        "total_cost",
        "baseline_cost",
        "cumulative_savings",
    ]
    pd.set_option("display.float_format", lambda x: f"{x:8.3f}")
    print("\n=== Mini-EMHASS schedule ===")
    print(df[show_cols].to_string(index=False))
    print(f"\nOptimized cost: {df['total_cost'].sum():.2f}")
    print(f"Baseline cost:  {df['baseline_cost'].sum():.2f}")
    print(f"Savings:        {df['cumulative_savings'].iloc[-1]:.2f}")


if __name__ == "__main__":
    main()
