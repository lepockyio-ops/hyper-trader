# 复核子 agent 任务模板（由 s03-research 用 sessions_spawn 派生，context 选 isolated）

你只做一件事：重跑指定任务并比较结果，不解读、不下结论。

输入：3 个任务编号（由主 agent 随机抽取）。
步骤：对每个任务运行矩阵脚本的 `verify --job <任务>`，原样记录输出的 JSON 行。
输出：一个表格，列为“任务 | match | stored | fresh”。任何 `match: false` 都要列出，不要尝试解释或修复。
