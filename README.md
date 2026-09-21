# fivewhys · 五问

> Agent-driven root cause analysis. Ask why five times.

给一个「线上出错了」的场景，agent 自己查日志、查指标、查配置，连续追问「为什么」，
直到定位真正的根因。

<!-- TODO(M8)：换掉下面这行徽章里的状态 -->
![status](https://img.shields.io/badge/status-M0%20scaffold-yellow)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 这是什么

<!-- TODO(M8-3)：用 3 句话说清楚：谁痛、痛在哪、这个项目怎么解决。
     写作模板：
     第一句 —— 现象（线上服务出问题，工程师要花几十分钟翻日志）
     第二句 —— 现有做法的缺陷（信息散落在日志/指标/配置/发布记录里）
     第三句 —— 本项目做了什么（让 agent 按 SRE 的真实排障路径多步调查，并给出可验证的根因）
-->

（占位）

## 为什么叫 fivewhys

5 Whys 是根因分析的经典方法：每得出一个原因，就继续追问它为什么会发生。

本项目把 5 Whys 直接用作 agent 的**主要停止条件**——而不是拍脑袋定一个 `max_steps = 10`。
追问到第 5 层、或者某一步无法再往下拆解时，agent 才停止调查。

## 快速开始

<!-- TODO(M1)：M1 跑通后回来填。至少要包含：
     git clone / 创建 venv / pip install -e ".[dev]" / 配置 .env / 一条能跑出结果的命令
-->

（占位）

## 架构

<!-- TODO(M5)：画一张图，说明 recorder → agent loop → tools → diagnosis 的数据流 -->

（占位）

## 评测结果

**M1 端到端验收**（1 个场景 × 5 次，`python scripts/demo_m1.py --runs 5`）

| 模型 | 根因定位准确率 | 平均工具调用次数 | 平均成本 | 平均耗时 | 通过判定 |
| ---- | -------------- | ---------------- | -------- | -------- | -------- |
| `deepseek/deepseek-chat`<sup>1</sup> | **100%**（5/5） | 8.0 | **$0.0031** | 13.8s | ✅（判定线 3/5） |

<sup>1</sup> 请求的模型名是 `deepseek/deepseek-chat`，DeepSeek 端实际返回的
模型 id 是 `deepseek-flash`。数字是 2026-09-10 用该 Key 实测的，命令与原始输出
见 [docs/TASKS.md](docs/TASKS.md) 的 FIV-5。

> **成本对照 NFR-2**：硬上限 $0.10/次 ✅ · 平均目标 $0.03/次 ✅（实测只有十分之一）。
> 按此推算 100 次全量评测约 $0.31，远低于 $5 的预算。

**完整改进曲线**（M6 的评测台跑起来之后往这里加行）

| 版本 | 根因定位准确率 | 平均工具调用次数 | 平均成本 | P95 延迟 |
| ---- | -------------- | ---------------- | -------- | -------- |
| baseline | — | — | — | — |
| + 结构化输出约束 | — | — | — | — |
| + 失败重试 | — | — | — | — |
| + 工具粒度优化 | — | — | — | — |
| + 步数 / 成本熔断 | — | — | — | — |

## 项目结构

```
fivewhys/
├── src/fivewhys/
│   ├── models.py        # 核心数据模型（地基）
│   ├── config.py        # 配置收口
│   ├── cli.py           # 命令行入口
│   ├── scenario.py      # 场景包：question + data + ground_truth
│   ├── snapshot.py      # 场景快照：评测集的字节级指纹
│   ├── agent/           # agent 主循环与提示词
│   ├── tools/           # 排障工具（当前按数据源切分；"按 SRE 路径切分更好"是待验证的假设）
│   └── mock/            # 可注入故障的模拟系统
├── eval/
│   └── scenario_snapshot.json  # 评测集指纹（进版本库；场景包本身是生成物）
├── tests/
├── benchmarks/          # 评测执行器与结果（M6）
└── docs/
    ├── REQUIREMENTS.md  # 需求说明：要什么、为什么、做到什么算完成
    ├── DESIGN.md        # 设计方案对比：每个目标的候选方案与取舍
    ├── ROADMAP.md       # 9 个目标与验收标准
    ├── TASKS.md         # 任务看板
    ├── WORKFLOW.md      # 每个任务的 7 步开发流程
    └── DEV.md           # 环境约定（不装 C 盘等）
```

## 文档体系

```
REQUIREMENTS.md   要什么、为什么          ← 上游，稳定
      ↓
DESIGN.md         怎么做、为什么这么选     ← 技术选型与取舍
      ↓
ROADMAP.md        9 个目标、验收标准
      ↓
TASKS.md          具体任务
      ↓
代码
```

**需求变了，往下三层都要跟着改。**

## 路线图

- [需求说明](docs/REQUIREMENTS.md) —— 先看这个，理解项目要解决什么问题
- [路线图](docs/ROADMAP.md) —— 九个里程碑与验收标准
- **当前进度：M0~M4 已完成，M1 端到端验收通过（5/5）**，下一步 M5（大脑加固）
  - M3 交付：5 种故障 + 1 个健康对照场景，以及评测集指纹校验
    （`fivewhys snapshot --check`）
  - ⚠️ M1 的「真实模型跑 5 次至少对 3 次」验收仍未做 —— 需要 API Key

## 相关项目（以及本项目的区别）

做调研时发现了两个活跃的相邻项目，这里明确区分一下：

| 项目 | 它做什么 | 和 fivewhys 的区别 |
| ---- | -------- | ------------------ |
| [faultline](https://pypi.org/project/faultline/) | 给 *你自己的* agent 注入故障，测试它的韧性 | 它是**测试 agent**，不是诊断系统 |
| [whatbroke](https://pypi.org/project/whatbroke/) | 对比环境快照（依赖 / 配置 / 镜像），找出什么变了 | 它是**对比**，不做多步推理，也不看日志和指标 |

<!-- TODO(M8)：把上面这段扩成 3-5 句，说清楚：
     "它们解决的是 X 和 Y，而 fivewhys 解决的是 Z"。面试时这段话会被问到。 -->

## License

MIT
