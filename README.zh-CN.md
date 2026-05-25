# 电池储能套利与 VPP 模拟器

> 用 [Pyomo](https://www.pyomo.org/) 写一个 24 小时电池调度优化器,在给定的分时电价下,
> 求"低买高卖"的最优充放电策略,最大化日内套利收益。

这是面向**储能算法 / VPP** 方向入门的练习项目。
现在包含三个层次:

1. `battery_optimizer.py`:最小电池套利 LP
2. `mini_emhass.py`:简化版家庭 EMS / Mini-EMHASS
3. `amber_vpp_simulator.py`:Amber / 自建 Home Assistant / 传统 VPP 收益对比模拟器

---

## 1. 项目结构

```
battery_optimizer/
├── battery_optimizer.py    # 主程序:建模 / 求解 / 出图
├── mini_emhass.py          # 简化版 EMHASS:PV/负荷/电池/电网/柔性负荷调度
├── amber_vpp_simulator.py  # Amber / VPP / 自建 HA 收益对比
├── config_mini_emhass.json # Mini-EMHASS 参数配置
├── requirements.txt        # Python 依赖
├── outputs/                # Amber/VPP 模拟器输出
├── schedule.csv            # 运行后生成 —— 24 小时调度明细
├── results.png             # 运行后生成 —— 三图合一
├── mini_emhass_schedule.csv
├── mini_emhass_results.png
├── mini_emhass_action.json
└── README.md
```

---

## 2. 快速运行

```bash
# 1. 进入项目
cd ~/projects/battery_optimizer

# 2. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 运行
python battery_optimizer.py
```

会在终端打印调度表,并生成 `schedule.csv` 和 `results.png`。

### 运行 Mini-EMHASS

Mini-EMHASS 是受 [EMHASS](https://github.com/davidusb-geek/emhass) 启发的学习版 EMS 工具。
原项目面向 Home Assistant,用线性规划优化住宅侧能源调度;这里保留核心数学结构,但用更小的代码实现:

- 光伏预测 `pv_kw`
- 家庭基础负荷 `base_load_kw`
- 买电价 `import_price`
- 卖电价 `export_price`
- 电池充放电
- 一个柔性负荷,例如 EV 充电或热水器
- 电网 import/export
- 输出 Home Assistant 风格动作 JSON

```bash
cd ~/projects/battery_optimizer
source venv/bin/activate
python mini_emhass.py --current-hour 10
```

运行后生成:

| 文件 | 作用 |
|---|---|
| `mini_emhass_schedule.csv` | 24 小时优化调度表 |
| `mini_emhass_results.png` | 电价、PV/负荷、电池 SOC、电网功率和累计节省图 |
| `mini_emhass_action.json` | 当前小时的控制动作,可类比 Home Assistant 自动化 payload |

示例结果:

| 指标 | 值 |
|---|---:|
| 优化后电费 | 约 5.55 |
| 不优化 baseline 电费 | 约 11.45 |
| 优化节省 | 约 5.91 |

你可以切换目标函数:

```bash
python mini_emhass.py --cost-function cost
python mini_emhass.py --cost-function profit
python mini_emhass.py --cost-function self-consumption
```

三种目标的含义:

| 目标 | 含义 |
|---|---|
| `cost` | 最小化电费,适合普通家庭账单优化 |
| `profit` | 最大化负电费/收益,适合 VPP 思维 |
| `self-consumption` | 优先减少电网交互和弃光,适合提高光伏自用率 |

### 运行 Amber / VPP 收益模拟器

这个脚本把之前关于 Amber、VPP、大型 BESS、小电池套利竞争的讨论落到代码里。
它比较三种策略:

| 模式 | 含义 |
|---|---|
| `self_ha` | 自己用 Home Assistant + 本地优化算法控制电池 |
| `amber_smartshift` | 模拟 Amber 批发价 import/export + SmartShift 类调度 |
| `fixed_vpp` | 模拟传统 VPP:固定补贴 + 极端日事件调度 |

默认跑夏季/冬季、10/20/40 kWh、三种策略:

```bash
cd ~/projects/battery_optimizer
source venv/bin/activate
python amber_vpp_simulator.py
```

输出文件:

| 文件 | 作用 |
|---|---|
| `outputs/amber_vpp_summary.csv` | 每种季节/策略/容量的收益汇总 |
| `outputs/amber_vpp_dispatch.csv` | 每小时 SOC、充放电、电网 import/export 明细 |
| `outputs/amber_vpp_comparison.png` | 策略收益对比图 |
| `outputs/amber_vpp_best_schedule.png` | 最优案例的详细调度图 |
| `outputs/amber_vpp_report.md` | 自动生成的结论报告 |

默认价格是 `conservative` 保守普通日,不会假设每天都有极端尖峰价。
如果想看极端高价日的上限收益,可以跑:

```bash
python amber_vpp_simulator.py --volatility spiky
```

也可以只看某个容量:

```bash
python amber_vpp_simulator.py --capacities 40
```

注意:这里的 Amber/VPP 数字是**模拟参数**,不是实时报价,也不是投资建议。
真正做项目时,下一步要换成 AEMO/NEM 价格、Amber 实际 import/export、smart meter 负荷、PV inverter 发电和电池 SOC 数据。

---

## 3. 模型

### 决策变量(逐小时,t = 0..23)

| 变量 | 含义 | 范围 |
|---|---|---|
| `p_ch[t]`  | 充电功率 (kW) | `[0, 5]` |
| `p_dis[t]` | 放电功率 (kW) | `[0, 5]` |
| `soc[t]`   | 时刻 t 的荷电状态 (kWh),共 25 个时刻点 | `[0, 10]` |

### 约束

- **初始 SOC**:`soc[0]  = 5 kWh`
- **末态闭环**:`soc[24] = 5 kWh` —— 保证策略可日复一日循环执行
- **能量平衡**:`soc[t+1] = soc[t] + p_ch[t]·η_c·Δt − p_dis[t]/η_d·Δt`
- **功率上限**:`p_ch ≤ 5, p_dis ≤ 5`
- **容量上下限**:`0 ≤ soc ≤ 10` (作为变量 bounds 直接写在声明里)

### 目标

```
max Σ_t  price[t] · (p_dis[t] − p_ch[t]) · Δt
```

放电卖电是正向现金流,充电买电是负向现金流。

### 为什么这是 LP,不需要 MILP?

效率 `η_c · η_d = 0.95 × 0.95 ≈ 0.90 < 1`。
如果某小时同时 `p_ch > 0` 且 `p_dis > 0`,白白损失能量,目标函数变差,
最优解天然不会这么干 —— 不需要 0/1 互斥变量。

如果效率取 100%,或在某些极端电价情形,LP 才可能出现"虚假双向流动",
那时就要引入 binary 变量(`y[t] ∈ {0,1}`,`p_ch ≤ M·y`,`p_dis ≤ M·(1-y)`),
模型升级为 MILP。

---

## 4. 关键参数

| 参数 | 值 | 备注 |
|---|---|---|
| 电池容量          | 10 kWh         |  |
| 最大充/放电功率   | 5 kW           | 0.5C |
| 充/放电效率       | 0.95 / 0.95    | round-trip ≈ 0.9025 |
| 初始 SOC          | 5 kWh (50%)    |  |
| 时间步长 Δt       | 1 h            |  |

### 模拟电价(¥/kWh)

| 时段 | 价格 | 类型 |
|---|---|---|
| 00:00 – 05:59 | 0.25 | 深谷 |
| 06:00         | 0.45 | 过渡 |
| 07:00 – 10:59 | 0.95 | 早高峰 |
| 11:00 – 17:59 | 0.60 | 平段 |
| 18:00 – 22:59 | 1.20 | 晚高峰 |
| 23:00         | 0.45 | 过渡 |

---

## 5. 运行结果(示例)

求解器在 < 0.1s 内完成,**日内套利收益 ≈ ¥10.26**。

最优策略一眼能看出来的几个特征:
1. **深谷 (0-5h) 几乎不动** —— 价格已经够低,但电池只有 10 kWh,得"省着用"
2. **5h 满功率充至 100%** —— 紧贴早高峰,中间空闲损耗最小
3. **7-8h 放电至 0%** —— 卖给早高峰
4. **16-17h 在平段 (0.6) 充满** —— 因为 `0.6 / 0.95 ≈ 0.63` 进电池,在晚高峰 `1.2 × 0.95 = 1.14` 卖出,套利空间 ≈ ¥0.51/kWh
5. **18-19h 放电至 0%** —— 卖给晚高峰
6. **23h 用过渡价 (0.45) 补回 5 kWh** —— 满足末态闭环约束

---

## 6. 下一步可以加什么

入门跑通后,真实 VPP 场景里往往再叠加:

- **不确定性**:电价或负荷预测有噪声 → 随机规划 (SP) / 鲁棒优化 (RO) / MPC 滚动优化
- **多市场**:同时参与现货 + 调频 + 备用 → 多目标 / 收益堆叠
- **多设备**:N 块电池 + 光伏 + 负荷 → 集合调度,Pyomo 的 `Set` 直接扩
- **电池退化成本**:把循环深度 / 等效循环次数折算成 ¥/kWh,加进目标函数
- **网损 / 节点电价**:从单母线套利升级到节点电价 (LMP) + 网络约束
- **滚动 MPC**:每小时重解,用前一时段的实际值作为新初始 SOC

每一个都是 VPP 工程里实打实的工作量。

---

## 7. Mini-EMHASS 模型解释

Mini-EMHASS 的核心功率平衡是:

```
grid_import + pv + battery_discharge
    = base_load + flexible_load + battery_charge + grid_export + pv_curtailment
```

这相当于把家庭看成一个单母线系统:流入家庭能量总线的功率,必须等于流出的功率。

### 决策变量

| 变量 | 含义 |
|---|---|
| `grid_import[t]` | 第 t 小时从电网买电 |
| `grid_export[t]` | 第 t 小时向电网卖电 |
| `p_batt_ch[t]` | 电池充电功率 |
| `p_batt_dis[t]` | 电池放电功率 |
| `soc[t]` | 电池 SOC |
| `p_flex[t]` | 柔性负荷功率 |
| `pv_curtail[t]` | 弃光功率 |

### 柔性负荷

`config_mini_emhass.json` 里默认有一个 EV charger:

```json
"deferrable_load": {
  "enabled": true,
  "name": "ev_charger",
  "energy_required_kwh": 6.0,
  "max_power_kw": 3.0,
  "earliest_hour": 10,
  "latest_hour": 17
}
```

意思是:在 10:00 到 17:00 之间,必须完成 6 kWh 充电,每小时最大 3 kW。
优化器会自动把它安排到 PV 多、价格低、系统整体更划算的小时。

### Home Assistant 风格输出

`mini_emhass_action.json` 例子:

```json
{
  "tool": "mini_emhass",
  "current_hour": 10,
  "battery_mode": "idle",
  "battery_setpoint_kw": 0.0,
  "setpoint_convention": "positive=charge, negative=discharge",
  "flexible_load_name": "ev_charger",
  "flexible_load_setpoint_kw": 3.0,
  "expected_grid_import_kw": 0.0,
  "expected_grid_export_kw": 0.43,
  "soc_target_end_kwh": 1.0
}
```

真实接入 Home Assistant 时,这类 JSON 可以通过 MQTT、REST API 或 sensor template 转成实体状态。

---

## 8. 依赖

- Python ≥ 3.9
- `pyomo` —— 优化建模 DSL
- `highspy` —— HiGHS 求解器(开源,目前最快的 LP/MILP 之一)
- `numpy / pandas / matplotlib`
