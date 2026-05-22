"""
Amber / VPP battery revenue simulator
=====================================

This script turns the previous market discussion into a runnable model.

Question:
    If a small battery cannot beat grid-scale BESS on pure wholesale arbitrage,
    which path is better for a household / small C&I battery?

Strategies compared:
    1. self_ha
       Home Assistant + local optimizer.
       Value mainly comes from PV self-consumption and avoiding retail imports.

    2. amber_smartshift
       Amber-like real-time wholesale import/export pricing.
       Value comes from volatility: charge when the wholesale signal is cheap,
       discharge/export when it is expensive.

    3. fixed_vpp
       Traditional VPP-style fixed daily credit plus event export incentives.
       Value is steadier, but upside is usually capped by the program design.

Important:
    All prices here are simulated learning assumptions, not live Amber, AEMO,
    Ausgrid, Endeavour, Essential, AGL, Origin, Tesla, or GloBird tariffs.
    Use this as a framework first, then replace the profiles with real data.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyomo.environ as pyo


HOURS = 24
DT_HOURS = 1.0


@dataclass(frozen=True)
class BatterySpec:
    """Physical battery parameters.

    The power rating scales with capacity using c_rate. For example:
        10 kWh * 0.5 C = 5 kW

    This is a common first-pass sizing assumption. Real products have their own
    inverter limits, so later you can replace this with product-specific data.
    """

    capacity_kwh: float
    c_rate: float = 0.5
    soc_min_fraction: float = 0.10
    soc_init_fraction: float = 0.50
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    degradation_cost_per_kwh: float = 0.015

    @property
    def p_max_kw(self) -> float:
        return self.capacity_kwh * self.c_rate

    @property
    def soc_min_kwh(self) -> float:
        return self.capacity_kwh * self.soc_min_fraction

    @property
    def soc_init_kwh(self) -> float:
        return self.capacity_kwh * self.soc_init_fraction


@dataclass(frozen=True)
class StrategyConfig:
    """Commercial assumptions for one operating mode."""

    mode: str
    import_price: np.ndarray
    export_price: np.ndarray
    daily_subscription_fee: float = 0.0
    daily_vpp_credit: float = 0.0
    explanation: str = ""


def tou_retail_price(season: str) -> np.ndarray:
    """Create a simple retail time-of-use price profile in AUD/kWh."""
    price = np.full(HOURS, 0.31)
    price[0:7] = 0.22
    price[7:15] = 0.30
    price[15:21] = 0.48 if season == "summer" else 0.42
    price[21:24] = 0.28
    return price


def wholesale_spot_price(season: str, volatility: str = "conservative") -> np.ndarray:
    """Create a stylized NEM-like wholesale signal in AUD/kWh.

    `conservative` is the default because it is safer for planning. It does not
    pretend that a scarcity spike happens every day.

    `spiky` is useful for stress-testing Amber/VPP upside on rare event days.
    """
    if volatility == "conservative" and season == "summer":
        price = np.array(
            [
                0.08, 0.07, 0.06, 0.05, 0.05, 0.06,
                0.08, 0.10, 0.08, 0.03, 0.00, -0.02,
                -0.01, 0.02, 0.05, 0.09, 0.14, 0.22,
                0.36, 0.48, 0.34, 0.22, 0.14, 0.10,
            ],
            dtype=float,
        )
    elif volatility == "conservative" and season == "winter":
        price = np.array(
            [
                0.10, 0.09, 0.08, 0.08, 0.09, 0.11,
                0.18, 0.24, 0.20, 0.15, 0.12, 0.09,
                0.08, 0.09, 0.11, 0.15, 0.22, 0.30,
                0.34, 0.30, 0.24, 0.18, 0.13, 0.11,
            ],
            dtype=float,
        )
    elif volatility == "spiky" and season == "summer":
        price = np.array(
            [
                0.09, 0.08, 0.07, 0.06, 0.06, 0.07,
                0.09, 0.12, 0.08, 0.03, -0.02, -0.04,
                -0.03, 0.00, 0.04, 0.10, 0.22, 0.55,
                1.20, 2.50, 0.80, 0.35, 0.18, 0.12,
            ],
            dtype=float,
        )
    elif volatility == "spiky" and season == "winter":
        price = np.array(
            [
                0.10, 0.09, 0.08, 0.08, 0.09, 0.11,
                0.22, 0.34, 0.28, 0.18, 0.12, 0.08,
                0.06, 0.07, 0.10, 0.18, 0.32, 0.48,
                0.55, 0.46, 0.34, 0.22, 0.15, 0.12,
            ],
            dtype=float,
        )
    else:
        raise ValueError(f"Unsupported season/volatility: {season}/{volatility}")

    return price


def load_profile(season: str) -> np.ndarray:
    """Household / small C&I demand profile in kW."""
    if season == "summer":
        return np.array(
            [
                0.8, 0.7, 0.7, 0.7, 0.8, 1.0,
                1.4, 1.8, 1.5, 1.2, 1.0, 0.9,
                0.9, 1.0, 1.2, 1.6, 2.2, 3.1,
                4.0, 4.3, 3.6, 2.6, 1.6, 1.0,
            ],
            dtype=float,
        )

    if season == "winter":
        return np.array(
            [
                1.0, 0.9, 0.9, 0.9, 1.1, 1.8,
                2.8, 3.2, 2.5, 1.5, 1.1, 1.0,
                1.0, 1.1, 1.3, 1.8, 2.8, 3.8,
                4.0, 3.4, 2.5, 1.8, 1.3, 1.1,
            ],
            dtype=float,
        )

    raise ValueError(f"Unsupported season: {season}")


def pv_profile(season: str) -> np.ndarray:
    """Solar PV forecast in kW."""
    hours = np.arange(HOURS)
    if season == "summer":
        sunrise, sunset, peak_kw = 5.5, 19.0, 6.0
    elif season == "winter":
        sunrise, sunset, peak_kw = 7.0, 17.0, 3.2
    else:
        raise ValueError(f"Unsupported season: {season}")

    daylight_fraction = (hours - sunrise) / (sunset - sunrise)
    pv = peak_kw * np.sin(np.pi * daylight_fraction)
    return np.clip(pv, 0.0, None)


def make_market_profile(season: str, volatility: str) -> pd.DataFrame:
    """Build one day of forecast data.

    Keeping all time-series signals in one DataFrame makes the code easy to
    inspect and easy to replace with real CSV/API data later.
    """
    hours = np.arange(HOURS)
    spot = wholesale_spot_price(season, volatility)

    return pd.DataFrame(
        {
            "hour": hours,
            "season": season,
            "volatility": volatility,
            "load_kw": load_profile(season),
            "pv_kw": pv_profile(season),
            "retail_import_price": tou_retail_price(season),
            "fixed_fit_price": np.full(HOURS, 0.08),
            "wholesale_spot_price": spot,
        }
    )


def top_event_hours(values: np.ndarray, count: int) -> np.ndarray:
    """Return a 0/1 mask for the highest-price event hours."""
    mask = np.zeros(HOURS)
    top_idx = np.argsort(values)[-count:]
    mask[top_idx] = 1.0
    return mask


def make_strategy(profile: pd.DataFrame, mode: str) -> StrategyConfig:
    """Translate a strategy name into import/export price arrays.

    Price convention:
        import_price > 0 means paying money to buy from the grid.
        export_price > 0 means receiving money for sending energy to the grid.
    """
    retail = profile["retail_import_price"].to_numpy()
    fixed_fit = profile["fixed_fit_price"].to_numpy()
    spot = profile["wholesale_spot_price"].to_numpy()
    season = str(profile["season"].iloc[0])

    if mode == "self_ha":
        return StrategyConfig(
            mode=mode,
            import_price=retail,
            export_price=fixed_fit,
            explanation="Local optimizer: maximize retail bill savings and fixed FiT export value.",
        )

    if mode == "amber_smartshift":
        # Amber-like assumption:
        #   export follows wholesale spot;
        #   import follows spot plus network/retail pass-through charges.
        #
        # This keeps import higher than export in the same interval, avoiding a
        # fake model loophole where the optimizer imports and exports instantly.
        network_and_retail_pass_through = 0.18
        daily_subscription = 22.0 / 30.0
        return StrategyConfig(
            mode=mode,
            import_price=spot + network_and_retail_pass_through,
            export_price=spot,
            daily_subscription_fee=daily_subscription,
            explanation="Amber-like: wholesale-linked import/export with a monthly subscription.",
        )

    if mode == "fixed_vpp":
        # Traditional VPP assumption:
        #   normal retail tariff most of the day;
        #   fixed daily participation credit;
        #   extra export value during a few called event hours.
        volatility = str(profile["volatility"].iloc[0])
        event_count = 3 if season == "summer" else 2
        event_mask = top_event_hours(spot, event_count)
        event_bonus = 0.55 if volatility == "spiky" else 0.0
        daily_credit = 240.0 / 365.0
        return StrategyConfig(
            mode=mode,
            import_price=retail,
            export_price=fixed_fit + event_bonus * event_mask,
            daily_vpp_credit=daily_credit,
            explanation="Fixed VPP: retail tariff plus fixed credit and event export bonus.",
        )

    raise ValueError(f"Unsupported mode: {mode}")


def build_dispatch_model(
    profile: pd.DataFrame,
    strategy: StrategyConfig,
    battery: BatterySpec,
    grid_import_limit_kw: float = 15.0,
    grid_export_limit_kw: float = 10.0,
) -> pyo.ConcreteModel:
    """Build a Pyomo MILP dispatch model.

    Why MILP here instead of the simpler LP used in battery_optimizer.py?
    --------------------------------------------------------------------
    When export prices can spike above import prices, a pure LP may invent
    simultaneous import/export or simultaneous charge/discharge within the same
    hourly interval. Real meters and inverters do not behave like that.

    Two binary variables avoid those artificial loops:
        is_importing[t] = 1  -> grid import allowed, export blocked
        is_charging[t]  = 1  -> battery charge allowed, discharge blocked

    This is still tiny: 24 hours * 2 binary variables = 48 binaries per run.
    HiGHS solves it quickly.
    """
    m = pyo.ConcreteModel(f"{strategy.mode}_{battery.capacity_kwh:g}kwh")
    m.T = pyo.RangeSet(0, HOURS - 1)
    m.T_soc = pyo.RangeSet(0, HOURS)

    # `load` is a reserved attribute name on Pyomo blocks, so use `demand`.
    m.demand = pyo.Param(m.T, initialize=profile["load_kw"].to_dict())
    m.pv = pyo.Param(m.T, initialize=profile["pv_kw"].to_dict())
    m.import_price = pyo.Param(m.T, initialize=dict(enumerate(strategy.import_price)))
    m.export_price = pyo.Param(m.T, initialize=dict(enumerate(strategy.export_price)))

    m.grid_import = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0.0, grid_import_limit_kw),
    )
    m.grid_export = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0.0, grid_export_limit_kw),
    )
    m.p_charge = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0.0, battery.p_max_kw),
    )
    m.p_discharge = pyo.Var(
        m.T,
        domain=pyo.NonNegativeReals,
        bounds=(0.0, battery.p_max_kw),
    )
    m.soc = pyo.Var(
        m.T_soc,
        domain=pyo.NonNegativeReals,
        bounds=(battery.soc_min_kwh, battery.capacity_kwh),
    )
    m.pv_curtail = pyo.Var(m.T, domain=pyo.NonNegativeReals)

    m.is_importing = pyo.Var(m.T, domain=pyo.Binary)
    m.is_charging = pyo.Var(m.T, domain=pyo.Binary)

    m.c_soc_init = pyo.Constraint(expr=m.soc[0] == battery.soc_init_kwh)
    m.c_soc_final = pyo.Constraint(expr=m.soc[HOURS] == battery.soc_init_kwh)

    def _battery_balance(m, t):
        return m.soc[t + 1] == (
            m.soc[t]
            + m.p_charge[t] * battery.charge_efficiency * DT_HOURS
            - m.p_discharge[t] / battery.discharge_efficiency * DT_HOURS
        )

    m.c_battery_balance = pyo.Constraint(m.T, rule=_battery_balance)

    def _home_balance(m, t):
        return (
            m.grid_import[t]
            + m.pv[t]
            + m.p_discharge[t]
            == m.demand[t]
            + m.p_charge[t]
            + m.grid_export[t]
            + m.pv_curtail[t]
        )

    m.c_home_balance = pyo.Constraint(m.T, rule=_home_balance)

    def _grid_direction(m, t):
        return m.grid_import[t] <= grid_import_limit_kw * m.is_importing[t]

    def _grid_reverse_direction(m, t):
        return m.grid_export[t] <= grid_export_limit_kw * (1 - m.is_importing[t])

    m.c_grid_import_only = pyo.Constraint(m.T, rule=_grid_direction)
    m.c_grid_export_only = pyo.Constraint(m.T, rule=_grid_reverse_direction)

    def _battery_charge_only(m, t):
        return m.p_charge[t] <= battery.p_max_kw * m.is_charging[t]

    def _battery_discharge_only(m, t):
        return m.p_discharge[t] <= battery.p_max_kw * (1 - m.is_charging[t])

    m.c_battery_charge_only = pyo.Constraint(m.T, rule=_battery_charge_only)
    m.c_battery_discharge_only = pyo.Constraint(m.T, rule=_battery_discharge_only)

    def _objective(m):
        energy_bill = sum(
            (
                m.import_price[t] * m.grid_import[t]
                - m.export_price[t] * m.grid_export[t]
            )
            * DT_HOURS
            for t in m.T
        )
        battery_wear = sum(
            battery.degradation_cost_per_kwh
            * (m.p_charge[t] + m.p_discharge[t])
            * DT_HOURS
            for t in m.T
        )
        return energy_bill + battery_wear

    m.obj = pyo.Objective(rule=_objective, sense=pyo.minimize)
    return m


def solve_model(model: pyo.ConcreteModel) -> None:
    """Solve one dispatch model with HiGHS."""
    solver = pyo.SolverFactory("appsi_highs")
    result = solver.solve(model)
    status = result.solver.termination_condition
    if str(status).lower() != "optimal":
        raise RuntimeError(f"Solver failed: {status}")


def clean_value(expr) -> float:
    """Convert a Pyomo value into a normal float and remove tiny solver noise."""
    value = float(pyo.value(expr))
    return 0.0 if abs(value) < 1e-8 else value


def no_battery_baseline_cost(profile: pd.DataFrame, strategy: StrategyConfig) -> float:
    """Cost for the same tariff with PV but without a battery."""
    net_load = profile["load_kw"].to_numpy() - profile["pv_kw"].to_numpy()
    grid_import = np.clip(net_load, 0.0, None)
    grid_export = np.clip(-net_load, 0.0, None)
    energy_cost = np.sum(
        strategy.import_price * grid_import - strategy.export_price * grid_export
    )

    # A household without a battery does not receive the VPP battery credit.
    vpp_credit = 0.0
    subscription = strategy.daily_subscription_fee
    return float(energy_cost + subscription - vpp_credit)


def extract_dispatch(
    model: pyo.ConcreteModel,
    profile: pd.DataFrame,
    strategy: StrategyConfig,
    battery: BatterySpec,
    season: str,
    volatility: str,
) -> pd.DataFrame:
    """Extract an optimized hourly schedule from Pyomo into pandas."""
    rows: List[Dict[str, float | str]] = []
    for t in range(HOURS):
        grid_import = clean_value(model.grid_import[t])
        grid_export = clean_value(model.grid_export[t])
        p_charge = clean_value(model.p_charge[t])
        p_discharge = clean_value(model.p_discharge[t])
        pv_curtail = clean_value(model.pv_curtail[t])
        import_price = float(strategy.import_price[t])
        export_price = float(strategy.export_price[t])

        energy_cost = (import_price * grid_import - export_price * grid_export) * DT_HOURS
        degradation_cost = (
            battery.degradation_cost_per_kwh * (p_charge + p_discharge) * DT_HOURS
        )

        rows.append(
            {
                "season": season,
                "volatility": volatility,
                "mode": strategy.mode,
                "capacity_kwh": battery.capacity_kwh,
                "hour": t,
                "load_kw": float(profile.loc[t, "load_kw"]),
                "pv_kw": float(profile.loc[t, "pv_kw"]),
                "spot_price": float(profile.loc[t, "wholesale_spot_price"]),
                "import_price": import_price,
                "export_price": export_price,
                "grid_import_kw": grid_import,
                "grid_export_kw": grid_export,
                "battery_charge_kw": p_charge,
                "battery_discharge_kw": p_discharge,
                "battery_net_kw": p_charge - p_discharge,
                "soc_start_kwh": clean_value(model.soc[t]),
                "soc_end_kwh": clean_value(model.soc[t + 1]),
                "pv_curtail_kw": pv_curtail,
                "energy_cost": energy_cost,
                "degradation_cost": degradation_cost,
                "hourly_cost": energy_cost + degradation_cost,
                "hourly_cashflow": -(energy_cost + degradation_cost),
            }
        )

    df = pd.DataFrame(rows)
    df["cumulative_cost_before_fixed_items"] = df["hourly_cost"].cumsum()
    return df


def run_one_case(
    season: str,
    mode: str,
    capacity_kwh: float,
    volatility: str,
) -> Tuple[pd.DataFrame, Dict[str, float | str]]:
    """Run one season/mode/capacity case and return dispatch + summary."""
    profile = make_market_profile(season, volatility)
    strategy = make_strategy(profile, mode)
    battery = BatterySpec(capacity_kwh=capacity_kwh)
    model = build_dispatch_model(profile, strategy, battery)
    solve_model(model)
    dispatch = extract_dispatch(model, profile, strategy, battery, season, volatility)

    variable_cost = float(dispatch["hourly_cost"].sum())
    optimized_cost = (
        variable_cost
        + strategy.daily_subscription_fee
        - strategy.daily_vpp_credit
    )
    baseline_cost = no_battery_baseline_cost(profile, strategy)
    savings = baseline_cost - optimized_cost

    summary = {
        "season": season,
        "volatility": volatility,
        "mode": mode,
        "capacity_kwh": capacity_kwh,
        "baseline_cost_per_day": baseline_cost,
        "optimized_cost_per_day": optimized_cost,
        "savings_per_day": savings,
        "savings_per_month_30d": savings * 30.0,
        "savings_per_year_365d": savings * 365.0,
        "daily_subscription_fee": strategy.daily_subscription_fee,
        "daily_vpp_credit": strategy.daily_vpp_credit,
        "throughput_kwh": float(
            (dispatch["battery_charge_kw"] + dispatch["battery_discharge_kw"]).sum()
        ),
        "pv_curtail_kwh": float(dispatch["pv_curtail_kw"].sum()),
        "explanation": strategy.explanation,
    }

    dispatch["daily_subscription_fee"] = strategy.daily_subscription_fee / HOURS
    dispatch["daily_vpp_credit"] = strategy.daily_vpp_credit / HOURS
    dispatch["cumulative_net_value"] = (
        -dispatch["hourly_cost"].cumsum()
        - np.arange(1, HOURS + 1) * strategy.daily_subscription_fee / HOURS
        + np.arange(1, HOURS + 1) * strategy.daily_vpp_credit / HOURS
    )
    return dispatch, summary


def run_scenarios(
    seasons: Iterable[str],
    modes: Iterable[str],
    capacities: Iterable[float],
    volatility: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Run all requested combinations."""
    dispatch_frames = []
    summary_rows = []

    for season in seasons:
        for mode in modes:
            for capacity in capacities:
                dispatch, summary = run_one_case(season, mode, float(capacity), volatility)
                dispatch_frames.append(dispatch)
                summary_rows.append(summary)

    return pd.concat(dispatch_frames, ignore_index=True), pd.DataFrame(summary_rows)


def plot_comparison(summary: pd.DataFrame, output_path: Path) -> None:
    """Plot savings by season, strategy, and battery size."""
    seasons = list(summary["season"].drop_duplicates())
    modes = list(summary["mode"].drop_duplicates())
    capacities = sorted(summary["capacity_kwh"].drop_duplicates())

    fig, axes = plt.subplots(1, len(seasons), figsize=(13, 5), sharey=True)
    if len(seasons) == 1:
        axes = [axes]

    width = 0.23
    x = np.arange(len(capacities))

    for ax, season in zip(axes, seasons):
        season_df = summary[summary["season"] == season]
        for i, mode in enumerate(modes):
            values = []
            for capacity in capacities:
                row = season_df[
                    (season_df["mode"] == mode)
                    & (season_df["capacity_kwh"] == capacity)
                ]
                values.append(float(row["savings_per_month_30d"].iloc[0]))
            ax.bar(x + (i - 1) * width, values, width=width, label=mode)

        ax.axhline(0, color="black", linewidth=0.7)
        ax.set_title(f"{season.title()} monthly value")
        ax.set_xticks(x)
        ax.set_xticklabels([f"{c:g} kWh" for c in capacities])
        ax.set_ylabel("AUD / 30 days")
        ax.grid(True, axis="y", alpha=0.25)

    axes[0].legend(loc="upper left")
    fig.suptitle("Amber / VPP / Home Assistant Strategy Comparison")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    print(f"Comparison plot saved: {output_path}")


def choose_representative_case(summary: pd.DataFrame) -> Tuple[str, str, float]:
    """Pick the strongest case to show as a detailed schedule plot."""
    row = summary.sort_values("savings_per_day", ascending=False).iloc[0]
    return str(row["season"]), str(row["mode"]), float(row["capacity_kwh"])


def plot_schedule(dispatch: pd.DataFrame, output_path: Path) -> None:
    """Plot one representative dispatch schedule."""
    label = (
        f"{dispatch['season'].iloc[0]} | {dispatch['mode'].iloc[0]} | "
        f"{dispatch['capacity_kwh'].iloc[0]:g} kWh"
    )
    hours = dispatch["hour"]

    fig, axes = plt.subplots(4, 1, figsize=(12, 12), sharex=True)

    ax = axes[0]
    ax.step(hours, dispatch["spot_price"], where="post", label="Wholesale spot", linewidth=2)
    ax.step(hours, dispatch["import_price"], where="post", label="Import price", linewidth=2)
    ax.step(hours, dispatch["export_price"], where="post", label="Export price", linewidth=2)
    ax.set_title(f"Price Signals ({label})")
    ax.set_ylabel("AUD/kWh")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")

    ax = axes[1]
    ax.plot(hours, dispatch["load_kw"], label="Load", linewidth=2)
    ax.plot(hours, dispatch["pv_kw"], label="PV", linewidth=2)
    ax.fill_between(hours, 0, dispatch["pv_curtail_kw"], alpha=0.25, label="PV curtail")
    ax.set_title("Load and PV Forecast")
    ax.set_ylabel("kW")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")

    ax = axes[2]
    ax.bar(hours, dispatch["battery_charge_kw"], label="Charge", alpha=0.8)
    ax.bar(hours, -dispatch["battery_discharge_kw"], label="Discharge", alpha=0.8)
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_title("Battery Dispatch and SOC")
    ax.set_ylabel("Battery kW")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")

    ax_soc = ax.twinx()
    soc_x = list(hours) + [HOURS]
    soc_y = [dispatch["soc_start_kwh"].iloc[0]] + list(dispatch["soc_end_kwh"])
    ax_soc.plot(soc_x, soc_y, color="#2a9d8f", marker="o", linewidth=2, label="SOC")
    ax_soc.set_ylabel("SOC kWh", color="#2a9d8f")
    ax_soc.tick_params(axis="y", labelcolor="#2a9d8f")
    ax_soc.legend(loc="upper right")

    ax = axes[3]
    ax.bar(hours, dispatch["grid_import_kw"], label="Grid import", alpha=0.8)
    ax.bar(hours, -dispatch["grid_export_kw"], label="Grid export", alpha=0.8)
    ax.plot(
        hours,
        dispatch["cumulative_net_value"],
        color="#e76f51",
        marker="o",
        linewidth=2,
        label="Cumulative net value",
    )
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_title("Grid Flow and Cumulative Net Value")
    ax.set_xlabel("Hour")
    ax.set_ylabel("kW / AUD")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")
    ax.set_xticks(range(0, 25, 2))

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    print(f"Schedule plot saved: {output_path}")


def markdown_table(df: pd.DataFrame, columns: List[str]) -> str:
    """Create a compact Markdown table without requiring tabulate."""
    table = df[columns].copy()
    for col in table.columns:
        if pd.api.types.is_float_dtype(table[col]):
            table[col] = table[col].map(lambda x: f"{x:.2f}")

    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = ["| " + " | ".join(str(v) for v in row) + " |" for row in table.to_numpy()]
    return "\n".join([header, sep] + rows)


def write_report(summary: pd.DataFrame, output_path: Path) -> None:
    """Write a short business/engineering interpretation."""
    ranked = summary.sort_values("savings_per_month_30d", ascending=False)
    best = ranked.iloc[0]
    report = f"""# Amber / VPP Battery Simulator Report

This is a simulated engineering model, not a tariff quote.

Volatility profile: `{best['volatility']}`

## Best Simulated Case

- Season: `{best['season']}`
- Mode: `{best['mode']}`
- Battery: `{best['capacity_kwh']:.0f} kWh`
- Estimated value: `{best['savings_per_month_30d']:.2f} AUD / 30 days`

## Full Comparison

{markdown_table(ranked, [
    'season',
    'volatility',
    'mode',
    'capacity_kwh',
    'baseline_cost_per_day',
    'optimized_cost_per_day',
    'savings_per_day',
    'savings_per_month_30d',
    'savings_per_year_365d',
    'throughput_kwh',
])}

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
"""
    output_path.write_text(report, encoding="utf-8")
    print(f"Report saved: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare self-HA, Amber, and VPP battery strategies.")
    parser.add_argument(
        "--seasons",
        nargs="+",
        default=["summer", "winter"],
        choices=["summer", "winter"],
        help="Seasons to simulate.",
    )
    parser.add_argument(
        "--capacities",
        nargs="+",
        type=float,
        default=[10.0, 20.0, 40.0],
        help="Battery capacities in kWh.",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["self_ha", "amber_smartshift", "fixed_vpp"],
        choices=["self_ha", "amber_smartshift", "fixed_vpp"],
        help="Strategies to compare.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Directory for CSV, PNG, and report outputs.",
    )
    parser.add_argument(
        "--volatility",
        default="conservative",
        choices=["conservative", "spiky"],
        help="Use conservative normal-day prices or a rare spiky event-day profile.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    dispatch, summary = run_scenarios(
        args.seasons,
        args.modes,
        args.capacities,
        args.volatility,
    )

    summary_path = output_dir / "amber_vpp_summary.csv"
    dispatch_path = output_dir / "amber_vpp_dispatch.csv"
    comparison_plot_path = output_dir / "amber_vpp_comparison.png"
    schedule_plot_path = output_dir / "amber_vpp_best_schedule.png"
    report_path = output_dir / "amber_vpp_report.md"

    summary.to_csv(summary_path, index=False)
    dispatch.to_csv(dispatch_path, index=False)
    print(f"Summary saved: {summary_path}")
    print(f"Dispatch saved: {dispatch_path}")

    plot_comparison(summary, comparison_plot_path)

    season, mode, capacity = choose_representative_case(summary)
    selected = dispatch[
        (dispatch["season"] == season)
        & (dispatch["mode"] == mode)
        & (dispatch["capacity_kwh"] == capacity)
    ]
    plot_schedule(selected, schedule_plot_path)
    write_report(summary, report_path)

    display_cols = [
        "season",
        "volatility",
        "mode",
        "capacity_kwh",
        "baseline_cost_per_day",
        "optimized_cost_per_day",
        "savings_per_day",
        "savings_per_month_30d",
        "savings_per_year_365d",
    ]
    pd.set_option("display.float_format", lambda x: f"{x:8.2f}")
    print("\n=== Strategy comparison ===")
    print(summary[display_cols].sort_values("savings_per_month_30d", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
