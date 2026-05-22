"""
电池储能套利优化器 (Battery Arbitrage Optimizer)
==================================================

场景:10 kWh 电池,24 小时,给定逐小时电价,
     求"最大化日内套利收益"的充放电策略。

数学模型(线性规划 LP):
--------------------------------------------------
决策变量 (每小时一个):
    p_ch[t]   ≥ 0           充电功率 (kW)
    p_dis[t]  ≥ 0           放电功率 (kW)
    soc[t]    ∈ [0, Cap]    荷电状态 (kWh),  t = 0..24

约束:
    soc[t+1] = soc[t] + p_ch[t]·η_c·Δt − p_dis[t]/η_d·Δt     能量平衡
    soc[0]   = SOC_INIT                                       初始
    soc[24]  = SOC_INIT                                       周期性 (日内闭环)
    p_ch[t]  ≤ P_max_ch,    p_dis[t] ≤ P_max_dis              功率上限

目标:
    max  Σ_t  price[t] · (p_dis[t] − p_ch[t]) · Δt

为什么这是 LP 而不是 MILP?
    充放电都有效率损失,η_c·η_d < 1。如果某小时同时 p_ch>0 且 p_dis>0,
    净结果是白白损失能量,目标函数变差。所以最优解天然不会同时充放,
    不需要引入 0/1 互斥变量。这让模型保持线性,求解非常快。
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import pyomo.environ as pyo


# ============================================================================
# 1. 参数 (Parameters)
# ============================================================================
HOURS = 24

# --- 电池物理参数 ---
CAPACITY      = 10.0   # kWh   电池容量
P_MAX_CHARGE  = 5.0    # kW    最大充电功率 (0.5C)
P_MAX_DISCH   = 5.0    # kW    最大放电功率
ETA_CHARGE    = 0.95   # 充电效率
ETA_DISCHARGE = 0.95   # 放电效率   (round-trip ≈ 0.9025)
SOC_INIT      = 5.0    # kWh   初始荷电
SOC_MIN       = 0.0
SOC_MAX       = CAPACITY
DT            = 1.0    # h     时间步长


# ============================================================================
# 2. 模拟电价 (¥/kWh)
# ============================================================================
def make_prices() -> np.ndarray:
    """构造典型分时电价:深谷 / 平段 / 早晚双峰。"""
    p = np.full(HOURS, 0.60)   # 平段默认
    p[0:6]   = 0.25            # 00:00-05:59  深谷
    p[6]     = 0.45            # 06:00        过渡
    p[7:11]  = 0.95            # 07:00-10:59  早高峰
    p[18:23] = 1.20            # 18:00-22:59  晚高峰
    p[23]    = 0.45            # 23:00        过渡
    return p


# ============================================================================
# 3. 构建 Pyomo 模型 (Build model)
# ============================================================================
def build_model(prices: np.ndarray) -> pyo.ConcreteModel:
    """根据给定的逐小时电价构建优化模型。"""
    m = pyo.ConcreteModel("battery_arbitrage")

    # --- 索引集合 ---
    # 决策时段 0..23 (一天 24 个小时,每小时一组决策)
    m.T_h = pyo.RangeSet(0, HOURS - 1)
    # SOC 时刻点 0..24 (一天 24 小时共 25 个时刻点;soc[24] 是第 24 小时末)
    m.T_soc = pyo.RangeSet(0, HOURS)

    # --- 参数 (从外部数组导入) ---
    m.price = pyo.Param(m.T_h, initialize=dict(enumerate(prices)))

    # --- 决策变量 ---
    # bounds 直接写在变量声明里,Pyomo 会自动加上 ≤/≥ 约束
    m.p_ch  = pyo.Var(m.T_h,   domain=pyo.NonNegativeReals, bounds=(0, P_MAX_CHARGE))
    m.p_dis = pyo.Var(m.T_h,   domain=pyo.NonNegativeReals, bounds=(0, P_MAX_DISCH))
    m.soc   = pyo.Var(m.T_soc, domain=pyo.NonNegativeReals, bounds=(SOC_MIN, SOC_MAX))

    # --- 约束 ---
    # 初始 SOC
    m.c_init = pyo.Constraint(expr=m.soc[0] == SOC_INIT)

    # 末态 SOC == 初始 SOC: 让 24h 策略"自封闭",才能反映真实可持续收益。
    # 否则求解器会作弊:把电池放空换钱,虚增日内收益。
    m.c_cyclic = pyo.Constraint(expr=m.soc[HOURS] == SOC_INIT)

    # 能量平衡(逐小时递推)
    def _balance(m, t):
        return m.soc[t + 1] == (
            m.soc[t]
            + m.p_ch[t]  * ETA_CHARGE    * DT
            - m.p_dis[t] / ETA_DISCHARGE * DT
        )
    m.c_balance = pyo.Constraint(m.T_h, rule=_balance)

    # --- 目标:最大化日内套利现金流 ---
    def _revenue(m):
        return sum(m.price[t] * (m.p_dis[t] - m.p_ch[t]) * DT for t in m.T_h)
    m.obj = pyo.Objective(rule=_revenue, sense=pyo.maximize)

    return m


# ============================================================================
# 4. 求解 (Solve)
# ============================================================================
def solve(model: pyo.ConcreteModel):
    """调用 HiGHS 求解 LP 模型。"""
    # appsi_highs 是 Pyomo 自带、直接调 highspy 的接口(无需外部可执行文件)
    solver = pyo.SolverFactory("appsi_highs")
    result = solver.solve(model)

    tc = result.solver.termination_condition
    print(f"求解状态: {tc}")
    print(f"目标函数值(日内套利收益): ¥{pyo.value(model.obj):.2f}")
    return result


# ============================================================================
# 5. 提取结果 (Extract results into DataFrame)
# ============================================================================
def extract(model: pyo.ConcreteModel, prices: np.ndarray) -> pd.DataFrame:
    """把 Pyomo 变量值拉出来,放进 pandas DataFrame 方便后续分析/画图。"""
    rows = []
    for t in range(HOURS):
        p_ch  = pyo.value(model.p_ch[t])
        p_dis = pyo.value(model.p_dis[t])
        rows.append({
            "hour":     t,
            "price":    float(prices[t]),
            "p_ch":     p_ch,
            "p_dis":    p_dis,
            "p_net":    p_dis - p_ch,                          # 净放电 (正=卖,负=买)
            "soc_start": pyo.value(model.soc[t]),              # 小时初 SOC
            "soc_end":   pyo.value(model.soc[t + 1]),          # 小时末 SOC
            "cashflow": float(prices[t]) * (p_dis - p_ch) * DT,
        })
    df = pd.DataFrame(rows)
    df["cum_revenue"] = df["cashflow"].cumsum()
    return df


# ============================================================================
# 6. 可视化 (Plot)
# ============================================================================
def plot_results(df: pd.DataFrame, savepath: str):
    """三图合一:电价 / 调度+SOC / 累计收益。"""
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)

    # --- (a) 电价 ---
    ax = axes[0]
    ax.step(df["hour"], df["price"], where="post", color="#444", linewidth=2)
    ax.fill_between(df["hour"], 0, df["price"], step="post", alpha=0.15, color="#444")
    ax.set_ylabel("Price (¥/kWh)")
    ax.set_title("Time-of-Use Electricity Price")
    ax.grid(True, alpha=0.3)

    # --- (b) 充放电策略 + SOC (双 y 轴) ---
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

    # SOC 用副 y 轴叠加;x 用 0..24 的 25 个点(首点取 soc_start[0]、其后 soc_end)
    ax2 = ax.twinx()
    soc_x = list(df["hour"]) + [HOURS]
    soc_y = [df["soc_start"].iloc[0]] + list(df["soc_end"])
    ax2.plot(soc_x, soc_y, color="#2a9d8f", linewidth=2.2,
             marker="o", markersize=4, label="SOC (kWh)")
    ax2.set_ylabel("SOC (kWh)", color="#2a9d8f")
    ax2.tick_params(axis="y", labelcolor="#2a9d8f")
    ax2.set_ylim(0, CAPACITY * 1.05)
    ax2.legend(loc="upper right")

    # --- (c) 累计收益 ---
    ax = axes[2]
    ax.plot(df["hour"], df["cum_revenue"], color="#f4a261",
            linewidth=2.2, marker="o", markersize=4)
    ax.fill_between(df["hour"], 0, df["cum_revenue"], alpha=0.2, color="#f4a261")
    ax.axhline(0, color="black", linewidth=0.5)
    ax.set_xlabel("Hour")
    ax.set_ylabel("Cumulative Revenue (¥)")
    ax.set_title(f"Cumulative Arbitrage Revenue (Total: ¥{df['cashflow'].sum():.2f})")
    ax.grid(True, alpha=0.3)
    ax.set_xticks(range(0, 25, 2))

    plt.tight_layout()
    plt.savefig(savepath, dpi=140)
    print(f"图已保存: {savepath}")


# ============================================================================
# 入口
# ============================================================================
def main():
    prices = make_prices()
    model  = build_model(prices)
    solve(model)
    df = extract(model, prices)

    # 打印调度明细
    print("\n=== 最优调度表 ===")
    pd.set_option("display.float_format", lambda x: f"{x:7.3f}")
    show_cols = ["hour", "price", "p_ch", "p_dis", "soc_end", "cashflow", "cum_revenue"]
    print(df[show_cols].to_string(index=False))

    df.to_csv("schedule.csv", index=False)
    print("\n调度明细已保存: schedule.csv")

    plot_results(df, "results.png")


if __name__ == "__main__":
    main()
