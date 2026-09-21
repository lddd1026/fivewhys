"""可注入故障的模拟系统。

为什么不用真实系统？
  因为我们**必须掌握 ground truth**。用真系统就没法知道「正确答案」是什么，
  没有正确答案就没有自动判分，整个项目就退化成 demo。

五类数据，各有各的分工：

- :class:`LogStore`     —— 日志：「发生了什么」
- :class:`MetricStore`  —— 指标：「影响有多大」（**与日志同源**）
- :class:`ConfigStore`  —— 配置历史：「**根因在这里**」
- :class:`DeployStore`  —— 发布历史：「什么时候动过手」
- :class:`MockSystem`   —— 拓扑 + 跨服务流量生成

配置这一类尤其关键：真正的答案（``db.pool_size`` 50 -> 5）**不在日志里**，
日志只说「config reloaded」。agent 必须先怀疑到「配置变过」，才会去查它。

M1 只用 MockService（一个服务）。M2 起用其余部分。
"""

from fivewhys.mock.changes import (
    ConfigChange,
    ConfigSnapshot,
    ConfigStore,
    DeployRecord,
    DeployStore,
)
from fivewhys.mock.injectors import (
    INJECTORS,
    InjectionContext,
    InjectorSpec,
    available,
    catalogue,
    inject,
)
from fivewhys.mock.logstore import LogStore
from fivewhys.mock.metrics import MetricBucket, MetricStore, RequestSample
from fivewhys.mock.service import MockService
from fivewhys.mock.topology import (
    DEFAULT_CONFIGS,
    DEFAULT_TOPOLOGY,
    MockSystem,
    ServiceSpec,
)

__all__ = [
    "DEFAULT_CONFIGS",
    "DEFAULT_TOPOLOGY",
    "INJECTORS",
    "ConfigChange",
    "ConfigSnapshot",
    "ConfigStore",
    "DeployRecord",
    "DeployStore",
    "InjectionContext",
    "InjectorSpec",
    "LogStore",
    "MetricBucket",
    "MetricStore",
    "MockService",
    "MockSystem",
    "RequestSample",
    "ServiceSpec",
    "available",
    "catalogue",
    "inject",
]
