"""pytest 全局配置。

这里做两件事：

1. **在任何测试模块 import litellm 之前**，把 litellm 的在线价格表关掉。
2. 默认**关掉轨迹落盘** —— 测试不该往仓库里写运行时数据。

## 为什么要在这里关 litellm 的在线价格表

``fivewhys.agent.llm`` 在模块级也设了这个开关，
但如果某个测试模块先 ``import litellm``，模块级的设置就来不及生效 ——
litellm 一被 import 就开始拉 GitHub 上的价格表，网络不通时重试 3 次，
整套测试会从 2 秒变成 68 秒。

conftest.py 由 pytest 在收集测试之前导入，所以这里是唯一的「最早时机」。
这个坑是跑 ``pytest --durations`` 时发现的。

## 为什么要关轨迹落盘

需求 FR-9 要求每次诊断都落盘轨迹，所以**默认是开的**。
但测试要跑几十次诊断，如果都往 ``runs/`` 里写，仓库目录会被运行时数据淹没。
要测轨迹本身的行为（FIV-19）时，用例自己传
``trace=TraceWriter(..., root=tmp_path)`` —— 见 ``tests/test_trace.py``。
"""

from __future__ import annotations

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("FIVEWHYS_TRACE_ENABLED", "false")
