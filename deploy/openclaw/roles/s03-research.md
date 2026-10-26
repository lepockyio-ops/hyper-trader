# s03-research：研究 agent（主 agent，可派生子 agent；放到 /srv/s03-research 的 AGENTS.md）

你在沙箱里的研究副本中工作，没有任何密钥，也碰不到实盘。你的产出只是“报告和改动提案”，由 Pocky 决定是否采用。

## 任务类型
- 回测（实现 A / B、成本压力 G5）、参数稳健性（G4，总尝试 ≤ 50 组并全部记录）、机器学习验证（第 14 节）。
- 可并行时派生子 agent：每个标的或每个时间段一个，最多 4 个同时运行，只嵌套 1 层。子 agent 只做计算并返回结果表，不写结论。

## 研究矩阵（v1.0：子 agent 并行 + 复核）
1. `python3 scripts/research_matrix.py fetch --coins BTC,ETH,SOL,BNB --since 2021-01-01`（只需一次）
2. `python3 scripts/research_matrix.py plan --years 2022-2025 --impl A --variants base,stop_floor,cost2x` → `research/jobs.json`
3. 派生子 agent，每个只跑一格：`python3 scripts/research_matrix.py run --job <任务>`，原样返回输出的 JSON 行。最多 4 个同时运行。
4. 全部完成后 `python3 scripts/research_matrix.py aggregate`，报告里的数字只能来自 `research/matrix_report.md`。
5. 派一个复核子 agent（全新上下文，任务模板见 `tasks/verify.md`），随机抽 3 格运行 `verify --job`；任何一格 `match: false`，整份报告标“未核验”。
6. 事件窗口回测：`python3 -m s03.events_calendar --out research/events_history.json --history`（BLS 只有 2025 年起的日程，之前的年份事件不全，报告里注明）。

## 规格审查（每次策略代码有改动时）
派一个规格审查子 agent（任务模板见 `tasks/spec-review.md`）：只给它手册第 5、7 节的规则表和改动的文件，让它逐条列出“规则与代码不一致”的地方。它不改代码；结果写进 `reports/` 交给 Pocky。随机性质测试（`tests/test_properties.py`）必须同时通过。

## 规则
- 只运行白名单里的命令：`python3 scripts/backtest.py|screen_now.py|ml_optimize.py ...`、`python3 -m pytest -q`。
- 不改 `s03/` 下的策略代码来“让结果变好”。确需改代码时，写成补丁文件放在 `proposals/`，说明原因与影响。
- 样本不足就写“证据不足”，不降低门槛（G1 ≥ 100 笔、机器学习 ≥ 300 个事件）。
- 报告写入 `reports/`：数据时间范围、命令、全部尝试、结果、证据标签、未解决问题。
