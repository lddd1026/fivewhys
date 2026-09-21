"""pytest 全局配置。

这里只做一件事：**在任何测试模块 import litellm 之前**，把 litellm 的
在线价格表关掉。

为什么必须在 conftest 里做：``fivewhys.agent.llm`` 在模块级也设了这个开关，
但如果某个测试模块先 ``import litellm``，模块级的设置就来不及生效 ——
litellm 一被 import 就开始拉 GitHub 上的价格表，网络不通时重试 3 次，
整套测试会从 2 秒变成 68 秒。

conftest.py 由 pytest 在收集测试之前导入，所以这里是唯一的「最早时机」。
这个坑是跑 ``pytest --durations`` 时发现的。
"""

from __future__ import annotations

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
