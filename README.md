# hyper-trader-framework

基于 **Hyperliquid 原生 API 的永续交易框架**。v2.0 通过策略插件连接交易规则与数据、风控、执行、回放、监控和运维，当前面向做多策略。

默认插件为 `BlankStrategy` 空白模板，**不会产生入场信号**。默认配置为 `mode = "shadow"`、`network = "testnet"`；shadow 使用主网行情模拟运行，不发送交易订单。仓库中的 `s03` 是沿用的包名与服务命名空间。

## 文档入口

- [v2.0 中文完整手册](%E4%BA%A4%E6%98%93%E6%A1%86%E6%9E%B6%E6%A8%A1%E6%9D%BF%E6%89%8B%E5%86%8C%EF%BC%88Hyperliquid%20%E6%B0%B8%E7%BB%AD%20%C2%B7%20%E7%AD%96%E7%95%A5%E7%95%99%E7%A9%BA%20v2.0%EF%BC%89.md)：交易流程、数据、验证门槛、执行与运维。
- [框架说明](docs/FRAMEWORK.md)：策略插件与框架分工、交易循环。
- [策略规格模板](docs/STRATEGY_TEMPLATE.md)：策略规则、参数来源和验收标准。
- [部署说明](deploy/README.md)：Linux 服务、系统用户、密钥与 OpenClaw 权限。

## 框架提供什么

| 模块 | 内容 |
| --- | --- |
| 策略接口 | `StrategyPlugin`：市场筛选、入场意图、分批止盈与日线离场 |
| 数据 | Hyperliquid 行情、OKX 参考数据、行情录制与核对 |
| 风险 | 止损距离、单笔与总风险、相关性、仓位与杠杆、事件窗口、暂停与熔断 |
| 执行 | 入场与止损成组提交、订单编号、精度处理、持仓对账与看门狗 |
| 研究 | 影子模式、回放回测、研究矩阵与重跑复核 |
| 监控与复盘 | 行情告警、官方事件日历、复盘指标、日报与周报 |
| 可选扩展 | 机器学习风险门控、OpenClaw 告警分析与运维 |

## 本地安装

以下命令使用 Linux / macOS 的 Bash。代码使用标准库 `tomllib`，请使用 Python 3.11+；正式服务部署需要 Linux 与 systemd。

```bash
git clone https://github.com/lepockyio-ops/hyper-trader-framework.git
cd hyper-trader-framework
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config/settings.example.toml config/settings.toml
python -m pytest -q
```

核心 SDK 版本由 `requirements.txt` 固定为 `hyperliquid-python-sdk==0.24.0`。配置文件只放参数；账户地址和 API 钱包密钥通过环境变量或服务器部署环境提供。

配置加载顺序：显式路径参数 → `S03_SETTINGS` 环境变量 → `config/settings.toml`。

## 接入你的策略

1. 填写[策略规格模板](docs/STRATEGY_TEMPLATE.md)，明确筛选条件、信号、止损、离场和验证门槛。
2. 复制 [template.py](s03/strategies/template.py) 为自己的策略文件，继承 [StrategyPlugin](s03/strategy/api.py)。
3. 实现以下接口；完成后移除 `is_template = True` 或设为 `False`。

| 接口 | 策略负责的内容 |
| --- | --- |
| `context()` / `contexts()` | 根据已收盘日线判断标的是否具备交易条件 |
| `step()` | 返回挂单、改单或撤单意图，以及触发价、限价和绝对止损价 |
| `take_profit_legs()` | 可选：分批止盈价格与比例 |
| `on_daily_close()` | 可选：日线离场或减仓 |
| `ml_features()` | 可选：额外机器学习特征 |

4. 在 `config/settings.toml` 配置插件路径和参数：

```toml
[strategy]
plugin = "s03.strategies.my_strategy:MyStrategy"

[strategy.params]
# 填入自己的策略参数，在插件中用 self.p(...) 读取
```

以上片段应合并进已有配置，避免重复定义 TOML 表。当前主循环以 15 分钟信号周期运行；[均线示例插件](s03/strategies/examples/sma_cross.py)只用于理解接口和验证流程。

## 从研究到影子模式

在仓库根目录执行：

```bash
# 查看插件当前的日线判断；空白模板不会给出可交易信号
python scripts/screen_now.py

# 录制 Hyperliquid 行情
python scripts/run.py record

# 回放回测：A 为突破触发，B 为收盘确认后限价
python scripts/backtest.py --coin SOL --impl A
python scripts/backtest.py --coin SOL --impl B --fee-mult 2 --slip-mult 3

# 使用当前配置跑一个周期，或持续运行
python scripts/run.py once
python scripts/run.py loop
```

在保持默认 shadow 模式时，循环只模拟运行。空白策略运行后无交易是预期行为。

研究矩阵入口是 [scripts/research_matrix.py](scripts/research_matrix.py)，提供 `fetch`、`plan`、`run`、`verify`、`aggregate`。回放使用参考市场数据时，应核对其与实际交易市场的差异。

## 运行模式与上线验证

| 配置 | 行为 |
| --- | --- |
| `mode = "shadow"` | 使用主网行情模拟运行，不发送交易订单 |
| `mode = "live"`、`network = "testnet"` | 向测试网发送订单，需要测试网账户配置 |
| `mode = "live"`、`network = "mainnet"` | 主网交易，另受 `HL_ALLOW_MAINNET` 人工解锁检查约束 |

从回测、G0–G6 验证、影子模式开始，再核对代码一致性与测试网行为。手册要求的测试网检查包括：触发单开仓、只减仓止损、定时撤单对止损的影响、逐仓强平距离、成组订单的止损生效时机。

测试网检查入口为 `python scripts/testnet_checks.py --coin BTC`，**会涉及测试网订单操作**，运行前按手册准备账户并逐项检查。完成 G8 后，主网与 G9 小额实盘仍需人工授权；测试通过不代表策略盈利。

## 监控、复盘与运维

```bash
python scripts/run.py alerts
# 在另一个终端运行本机状态/暂停代理
python scripts/run.py gate

python scripts/run.py metrics --days 7
python scripts/run.py report --days 7
python scripts/run.py pause "人工检查"
```

`pause` 只暂停新开仓；`kill` 是不同的熔断操作，主循环随后会撤单并平仓。恢复操作应由人工按手册执行。

OpenClaw 告警接入默认关闭。告警分析端经代理可查看状态、暂停新开仓；运维角色的服务管理权限按部署白名单配置。监控、分析和交易密钥按角色隔离。

服务器安装见[部署说明](deploy/README.md)。文档与服务中沿用 `/opt/s03/hl-s03` 等路径，部署时应将本仓库放到对应位置，或一致地调整相关配置。可选机器学习模块默认关闭，候选模型需独立验证和人工批准。

## 目录导航

| 路径 | 用途 |
| --- | --- |
| [config/settings.example.toml](config/settings.example.toml) | 运行模式、策略、风险、成本与告警配置模板 |
| [s03/strategy/api.py](s03/strategy/api.py) | 插件接口、加载器与止损护栏 |
| [s03/strategies/](s03/strategies/) | 空白策略和示例插件 |
| [s03/data/](s03/data/) | 行情、参考数据与录制 |
| [s03/risk/](s03/risk/) | 仓位、杠杆与风控 |
| [s03/execution/](s03/execution/) | 下单、订单编号与精度 |
| [s03/live/](s03/live/) | 主循环、状态与看门狗 |
| [s03/research/](s03/research/) | 回放与成交模拟 |
| [s03/alerts/](s03/alerts/)、[s03/report/](s03/report/) | 告警与复盘 |
| [s03/ml/](s03/ml/) | 可选机器学习风险门控 |
| [scripts/](scripts/)、[deploy/](deploy/)、[tests/](tests/) | 命令入口、服务部署与测试 |

## 使用边界

- 风险、成本、滑点与相关性阈值是待验证的配置起点，应按策略和实际账户校准。
- 回放、影子成交与真实成交可能不同；订单行为、精度、动作额度和止损链路需要目标环境验证。
- 外部事件日历、行情与参考数据依赖网络可用性；运行前检查数据新鲜度。
- API 钱包密钥和本地配置不得提交到 Git。空白模板及均线示例均不代表可盈利策略。
