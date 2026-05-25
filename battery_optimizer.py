"""
Battery Arbitrage Optimizer
===========================

Scenario:
    A 10 kWh battery is scheduled over 24 hourly intervals. Given an hourly
    electricity price profile, the optimizer chooses charge/discharge power to
    maximize daily arbitrage revenue.

Mathematical model: linear programming (LP)
-------------------------------------------

Decision variables:
    p_ch[t]   >= 0          charge power (kW)
    p_dis[t]  >= 0          discharge power (kW)
    soc[t]    in [0, Cap]   state of charge (kWh), t = 0..24

Constraints:
    soc[t+1] = soc[t] + p_ch[t] * eta_c * dt - p_dis[t] / eta_d * dt
    soc[0]   = initial SOC
    soc[24]  = initial SOC, so the daily schedule is cyclic
    p_ch[t]  <= p_charge_max
    p_dis[t] <= p_discharge_max

Objective:
    maximize sum_t price[t] * (p_dis[t] - p_ch[t]) * dt

Why this is an LP instead of a MILP:
    Round-trip efficiency is below 100%. If the model charges and discharges at
    the same time, it loses energy and worsens the objective. Therefore, under
    this simple arbitrage setting, the optimum naturally avoids simultaneous
    charge/discharge without binary variables.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import pyomo.environ as pyo


# ============================================================================
# 1. Parameters
# ============================================================================
HOURS = 24

# --- Battery physical parameters ---
CAPACITY      = 10.0   # kWh   battery capacity
P_MAX_CHARGE  = 5.0    # kW    maximum charge power (0.5C)
P_MAX_DISCH   = 5.0    # kW    maximum discharge power
ETA_CHARGE    = 0.95   # charge efficiency
ETA_DISCHARGE = 0.95   # discharge efficiency (round-trip is about 0.9025)
SOC_INIT      = 5.0    # kWh   initial state of charge
SOC_MIN       = 0.0
SOC_MAX       = CAPACITY
DT            = 1.0    # h     time step


# ============================================================================
# 2. Simulated time-of-use price
# ============================================================================
def make_prices() -> np.ndarray:
    """Create a typical low/shoulder/high time-of-use price profile."""
    p = np.full(HOURS, 0.60)   # default shoulder price
    p[0:6]   = 0.25            # 00:00-05:59 low overnight price
    p[6]     = 0.45            # 06:00 transition
    p[7:11]  = 0.95            # 07:00-10:59 morning peak
    p[18:23] = 1.20            # 18:00-22:59 evening peak
    p[23]    = 0.45            # 23:00 transition
    return p


# ============================================================================
# 3. Build the Pyomo model
# ============================================================================
def build_model(prices: np.ndarray) -> pyo.ConcreteModel:
    """Build an optimization model from the hourly price vector."""
    m = pyo.ConcreteModel("battery_arbitrage")

    # --- Index sets ---
    # Decision intervals 0..23: one charge/discharge decision per hour.
    m.T_h = pyo.RangeSet(0, HOURS - 1)
    # SOC time points 0..24: 24 intervals require 25 state points.
    m.T_soc = pyo.RangeSet(0, HOURS)

    # --- Parameters imported from the price array ---
    m.price = pyo.Param(m.T_h, initialize=dict(enumerate(prices)))

    # --- Decision variables ---
    # Bounds are attached directly to variables for compact model generation.
    m.p_ch  = pyo.Var(m.T_h,   domain=pyo.NonNegativeReals, bounds=(0, P_MAX_CHARGE))
    m.p_dis = pyo.Var(m.T_h,   domain=pyo.NonNegativeReals, bounds=(0, P_MAX_DISCH))
    m.soc   = pyo.Var(m.T_soc, domain=pyo.NonNegativeReals, bounds=(SOC_MIN, SOC_MAX))

    # --- Constraints ---
    # Initial SOC
    m.c_init = pyo.Constraint(expr=m.soc[0] == SOC_INIT)

    # Final SOC equals initial SOC. This makes the daily strategy cyclic and
    # avoids overstating revenue by emptying the battery at the end of the day.
    m.c_cyclic = pyo.Constraint(expr=m.soc[HOURS] == SOC_INIT)

    # Battery energy balance, propagated hour by hour.
    def _balance(m, t):
        return m.soc[t + 1] == (
            m.soc[t]
            + m.p_ch[t]  * ETA_CHARGE    * DT
            - m.p_dis[t] / ETA_DISCHARGE * DT
        )
    m.c_balance = pyo.Constraint(m.T_h, rule=_balance)

    # --- Objective: maximize daily arbitrage cashflow ---
    def _revenue(m):
        return sum(m.price[t] * (m.p_dis[t] - m.p_ch[t]) * DT for t in m.T_h)
    m.obj = pyo.Objective(rule=_revenue, sense=pyo.maximize)

    return m


# ============================================================================
# 4. Solve
# ============================================================================
def solve(model: pyo.ConcreteModel):
    """Solve the LP model with HiGHS."""
    # appsi_highs calls the Python highspy package, so no external solver binary
    # is required.
    solver = pyo.SolverFactory("appsi_highs")
    result = solver.solve(model)

    tc = result.solver.termination_condition
    print(f"Solve status: {tc}")
    print(f"Objective value (daily arbitrage revenue): {pyo.value(model.obj):.2f}")
    return result


# ============================================================================
# 5. Extract results into a DataFrame
# ============================================================================
def extract(model: pyo.ConcreteModel, prices: np.ndarray) -> pd.DataFrame:
    """Convert Pyomo variable values into a pandas DataFrame."""
    rows = []
    for t in range(HOURS):
        p_ch  = pyo.value(model.p_ch[t])
        p_dis = pyo.value(model.p_dis[t])
        rows.append({
            "hour":     t,
            "price":    float(prices[t]),
            "p_ch":     p_ch,
            "p_dis":    p_dis,
            "p_net":    p_dis - p_ch,                          # positive means net discharge
            "soc_start": pyo.value(model.soc[t]),              # SOC at start of hour
            "soc_end":   pyo.value(model.soc[t + 1]),          # SOC at end of hour
            "cashflow": float(prices[t]) * (p_dis - p_ch) * DT,
        })
    df = pd.DataFrame(rows)
    df["cum_revenue"] = df["cashflow"].cumsum()
    return df


# ============================================================================
# 6. Plot
# ============================================================================
def plot_results(df: pd.DataFrame, savepath: str):
    """Create a three-panel chart: price, dispatch/SOC, cumulative revenue."""
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)

    # --- (a) Price ---
    ax = axes[0]
    ax.step(df["hour"], df["price"], where="post", color="#444", linewidth=2)
    ax.fill_between(df["hour"], 0, df["price"], step="post", alpha=0.15, color="#444")
    ax.set_ylabel("Price ($/kWh)")
    ax.set_title("Time-of-Use Electricity Price")
    ax.grid(True, alpha=0.3)

    # --- (b) Charge/discharge schedule + SOC on a secondary y-axis ---
    ax = axes[1]
    ax.bar(df["hour"],  df["p_ch"],  width=0.8, color="#3a86ff",
           label="Charge (kW)",    alpha=0.85)
    ax.bar(df["hour"], -df["p_dis"], width=0.8, color="#ef476f",
           label="Discharge (kW)", alpha=0.85)
    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_ylabel("Power (kW)")
    ax.set_title("Optimal Charge/Discharge Schedule + SOC")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)

    # SOC uses 25 points: the initial SOC plus the 24 end-of-hour values.
    ax2 = ax.twinx()
    soc_x = list(df["hour"]) + [HOURS]
    soc_y = [df["soc_start"].iloc[0]] + list(df["soc_end"])
    ax2.plot(soc_x, soc_y, color="#2a9d8f", linewidth=2.2,
             marker="o", markersize=4, label="SOC (kWh)")
    ax2.set_ylabel("SOC (kWh)", color="#2a9d8f")
    ax2.tick_params(axis="y", labelcolor="#2a9d8f")
    ax2.set_ylim(0, CAPACITY * 1.05)
    ax2.legend(loc="upper right")

    # --- (c) Cumulative revenue ---
    ax = axes[2]
    ax.plot(df["hour"], df["cum_revenue"], color="#f4a261",
            linewidth=2.2, marker="o", markersize=4)
    ax.fill_between(df["hour"], 0, df["cum_revenue"], alpha=0.2, color="#f4a261")
    ax.axhline(0, color="black", linewidth=0.5)
    ax.set_xlabel("Hour")
    ax.set_ylabel("Cumulative Revenue ($)")
    ax.set_title(f"Cumulative Arbitrage Revenue (Total: ${df['cashflow'].sum():.2f})")
    ax.grid(True, alpha=0.3)
    ax.set_xticks(range(0, 25, 2))

    plt.tight_layout()
    plt.savefig(savepath, dpi=140)
    print(f"Chart saved: {savepath}")


# ============================================================================
# Entry point
# ============================================================================
def main():
    prices = make_prices()
    model  = build_model(prices)
    solve(model)
    df = extract(model, prices)

    # Print dispatch details.
    print("\n=== Optimal dispatch table ===")
    pd.set_option("display.float_format", lambda x: f"{x:7.3f}")
    show_cols = ["hour", "price", "p_ch", "p_dis", "soc_end", "cashflow", "cum_revenue"]
    print(df[show_cols].to_string(index=False))

    df.to_csv("schedule.csv", index=False)
    print("\nDispatch table saved: schedule.csv")

    plot_results(df, "results.png")


if __name__ == "__main__":
    main()
