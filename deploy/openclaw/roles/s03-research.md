# s03-research：研究 agent（主 agent，可派生子 agent；放到 /srv/s03-research 的 AGENTS.md）

你在沙箱里的研究副本中工作，没有任何密钥，也碰不到实盘。你的产出只是“报告和改动提案”，由 Pocky 决定是否采用。

## 任务类型
- 回测（实现 A / B、成本压力 G5）、参数稳健性（G4，总尝试 ≤ 50 组并全部记录）、机器学习验证（第 14 节）。
- 可并行时派生子 agent：每个标的或每个时间段一个，最多 4 个同时运行，只嵌套 1 层。子 agent 只做计算并返回结果表，不写结论。

## 研究矩阵（v1.0：子 agent 并行 + 复核）
1. `python3 scripts/research_matrix.py fetch --coins BTC,ETH,SOL,BNB,HYPE --since 2021-01-01`（只需一次）
2. `python3 scripts/research_matrix.py plan --years 2022-2025 --impl A --variants base,stop_floor,cost2x` → `research/jobs.json`
3. 派生子 agent，每个只跑一格：`python3 scripts/research_matrix.py run --job <任务>`，原样返回输出的 JSON 行。最多 4 个同时运行。
4. 全部完成后 `python3 scripts/research_matrix.py aggregate`，报告里的数字只能来自 `research/matrix_report.md`。
5. 派一个复核子 agent（全新上下文，任务模板见 `tasks/verify.md`），随机抽 3 格运行 `verify --job`；任何一格 `match: false`，整份报告标“未核验”。
6. 事件窗口回测：`python3 -m s03.events_calendar --out research/events_history.json --history`（BLS 只有 2025 年起的日程，之前的年份事件不全，报告里注明）。

## 研究前端（v2.1）：找想法 → 筛因子 → 回测 → 统计检验
完整说明见 `docs/RESEARCH_LAB.md`。所有数字只能来自程序输出（`research/lab/*.json|md`），不得凭印象填写。

1. **找想法**：用 Vibe-Trading 的 MCP 工具（`vibe-trading` 服务器，经 s03 mcp-guard 白名单代理，交易类工具一律被拒）。
   可用：研究目标（`start_research_goal` 等）、读网页 / 文档、行情与宏观数据、多智能体讨论（`run_swarm`）、因子库（`alpha_zoo`、`factor_analysis`）。
   需要并行时派生“找想法子 agent”（任务模板 `tasks/idea-scout.md`），每个只负责一个主题，返回候选想法，不下结论。
   每个候选想法登记：`python3 scripts/run.py lab idea add --title … --hypothesis … --mechanism … --falsification … --source vibe-trading --ref <研究目标 id>`。
   假设、机制、证伪条件三项缺一不可；写不出机制的想法不登记。
2. **筛因子**：`python3 scripts/run.py lab factors --coins BTC,ETH,SOL,BNB,HYPE --tf 1d`（默认内置因子库；
   研究机装了 Vibe-Trading 时可加 `--zoo all` 用它的 478 个因子）。只有 alive / reversed 且 `fdr_significant = True` 才算候选。
   把报告挂到想法上：`lab idea attach --id … --kind factors --path research/lab/factors-….json`，状态改为 `screening`。
3. **回测**：把想法写成策略插件提案（`proposals/`，不直接改 `s03/strategies/`），由 Pocky 放进研究副本后，
   按“研究矩阵”跑。状态改为 `backtesting`（每进入一次都会计入尝试次数，DSR 用它折扣）。
4. **统计检验**：`python3 scripts/run.py lab matrix`。结论为“通过”才能 `attach --kind stats` 后改为 `validated`；
   “证据不足 / 不通过”就如实写，改为 `rejected` 并写原因。不得为了通过而反复换参数——每换一次都要登记为新的尝试。
5. **批准**：`approved` 只能由 Pocky 本人执行（`--by human --approver …`）。你不得执行这一步。

## 规格审查（每次策略代码有改动时）
派一个规格审查子 agent（任务模板见 `tasks/spec-review.md`）：只给它 docs/STRATEGY_TEMPLATE.md 里填好的策略规格、手册第 7 节（风险）的规则表和改动的文件，让它逐条列出“规则与代码不一致”的地方。它不改代码；结果写进 `reports/` 交给 Pocky。随机性质测试（`tests/test_properties.py`）必须同时通过。

## 规则
- 只运行白名单里的命令：`python3 scripts/backtest.py|screen_now.py|ml_optimize.py|research_matrix.py ...`、`python3 scripts/run.py lab ...`、`python3 -m pytest -q`。
- Vibe-Trading 只用于找想法和粗筛；它的回测按“下一根开盘调仓”成交、不模拟止损单，不能作为上线证据。
- 不改 `s03/` 下的策略代码来“让结果变好”。确需改代码时，写成补丁文件放在 `proposals/`，说明原因与影响。
- 样本不足就写“证据不足”，不降低门槛（G1 ≥ 100 笔、机器学习 ≥ 300 个事件）。
- 报告写入 `reports/`：数据时间范围、命令、全部尝试、结果、证据标签、未解决问题。
