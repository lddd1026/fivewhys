# 任务看板

状态：`[ ]` 待办 · `[~]` 进行中 · `[x]` 已完成

**规则：一次只允许一个任务处于 `[~]`。**

---

## 进行中

（空）

---

## 待办

### M1 · 端到端最细竖切（当前里程碑）

目标：一个假服务、一种故障、一个工具，agent 能诊断出来。

- [ ] **FIV-1** 实现 `db_pool_exhausted` 故障注入 · `P0` · 约 2h
  - 涉及：`src/fivewhys/mock/scenarios.py`
  - 详见下方「任务详情」

- [ ] **FIV-2** 实现 `query_logs` 工具 · `P0` · 约 2h
  - 涉及：`src/fivewhys/tools/query_logs.py`

- [ ] **FIV-3** 把 `query_logs` 注册进 registry 并写单测 · `P0` · 约 1h
  - 涉及：`src/fivewhys/tools/__init__.py`、`tests/`

- [ ] **FIV-4** 实现 agent 主循环 · `P0` · 约 4h
  - 涉及：`src/fivewhys/agent/loop.py`
  - 前置：需要 `.env` 里配好 `DEEPSEEK_API_KEY`

- [ ] **FIV-5** 端到端手工验证 M1 · `P0` · 约 2h
  - 涉及：`scripts/demo_m1.py`（新建）
  - 验收：跑 5 次，至少 3 次给出正确根因

### M2 · 把模拟系统做真

- [ ] FIV-6 扩展到 3 个服务 + 依赖关系 · `P0`
- [ ] FIV-7 生成指标数据（QPS / 错误率 / P95） · `P0`
- [ ] FIV-8 加配置文件与发布历史 · `P1`
- [ ] FIV-9 定义 ground truth 落盘格式 · `P0`

### M3 · 故障库

- [ ] FIV-10 抽出统一的注入器接口 · `P0`
- [ ] FIV-11 再实现 4 种故障（共 5 种） · `P0`
- [ ] FIV-12 场景快照脚本（保证可复现） · `P0`

### M4 · 工具层补齐

- [ ] FIV-13 补齐 5 个工具 · `P0`
- [ ] FIV-14 工具描述打磨 · `P1`
- [ ] FIV-15 每个工具的单测 · `P0`

### M5 · Agent 循环加固

- [ ] FIV-16 litellm 多模型接入 · `P0`
- [ ] FIV-17 结构化输出校验 + 证据来源校验 · `P0`
- [ ] FIV-18 终止条件（5 Whys 深度 / max_steps / max_cost） · `P0`
- [ ] FIV-19 trace 落盘 · `P0`

### M6 · 评测台 【关键】

- [ ] FIV-20 评测执行器（N 场景 × M 次） · `P0`
- [ ] FIV-21 指标计算（准确率 / 成本 / 延迟 / 调用次数） · `P0`
- [ ] FIV-22 报告输出 · `P0`
- [ ] FIV-23 LLM 响应缓存（省钱 + 可复现） · `P0`

### M7 · 改进迭代 【项目价值全在这】

- [ ] FIV-24 结构化输出约束 → 跑全量 → 记录 · `P0`
- [ ] FIV-25 失败重试 → 跑全量 → 记录 · `P0`
- [ ] FIV-26 工具粒度优化 → 跑全量 → 记录 · `P0`
- [ ] FIV-27 步数 / 成本熔断 → 跑全量 → 记录 · `P0`

### M8 · 扩展与包装

- [ ] FIV-28 故障补到 20 种 · `P1`
- [ ] FIV-29 Docker 化 · `P1`
- [ ] FIV-30 README：数字表 + 架构图 + 取舍分析 · `P0`
- [ ] FIV-31 失败模式分析文档 · `P2`

---

## 已完成

- [x] **FIV-0b** 建立开发流程工具 · 已完成
  - `docs/WORKFLOW.md` —— 每个任务的 7 步流程
  - `docs/TASKS.md` —— 本文件，任务看板
  - `.github/PULL_REQUEST_TEMPLATE.md` —— 合并前自查模板
  - `scripts/check.ps1` —— 一键本地验证（ruff + pytest + mypy）

- [x] **FIV-0** M0 脚手架 · 已完成
  - 提交：`5590361 M0: 项目脚手架`

---

# 任务详情

## FIV-1 · 实现 `db_pool_exhausted` 故障注入

**优先级** `P0`　**预估** 2h　**分支** `feat/FIV-1-inject-db-pool`

### 背景

我们需要一个「会坏」的假系统，让 agent 有东西可诊断。

关键在于 **ground truth 必须由我们掌握**——用真实系统就没法知道正确答案，
没有正确答案就没有自动判分，整个项目会退化成 demo。

### 目标

让 `inject_db_pool_exhausted()` 能往日志里注入一组**只反映现象、不暴露答案**的记录。

### 要做的事

1. 读 `src/fivewhys/mock/scenarios.py` 头部注释，里面写了完整的日志规格
2. 实现 `inject_db_pool_exhausted()`，按时间顺序写入 4 类日志：
   - 配置重载（INFO，触发点）
   - 错误开始出现（ERROR，逐渐变密）
   - 连接等待时间飙升（WARN，指向连接池的关键线索）
   - 请求延迟超 SLO（WARN，稀疏）
3. 返回填好的 `GroundTruth`
4. 把 `tests/test_smoke.py::test_inject_db_pool_exhausted_not_implemented_yet`
   改成真正的断言

### 验收标准

- [ ] 文件末尾的自测命令能跑通，打印出 `GroundTruth` 和日志统计
- [ ] `store.stats()` 里 ERROR 数量在 **10~30** 之间
- [ ] 日志里**不出现** `pool` / `连接池` / `exhaust` 字样
- [ ] 时间戳严格递增
- [ ] 同样的输入跑两次，输出的日志完全一致（可复现）
- [ ] `pytest` 全绿

### 提示

- 用 `service.emit(level, message, ts)` 写日志
- 用 `service._rng` 取随机数，**不要用全局 `random`**——否则场景不可复现
- 时间要错开：配置重载必须**严格早于**错误开始出现

### 自查问题（提交前问自己）

1. 如果我是 agent，只看这些日志，能直接读出答案吗？能 → 说明写太直白了，重写。
2. 这些日志混在正常请求日志里，显眼吗？太显眼 → 说明噪声不够。
