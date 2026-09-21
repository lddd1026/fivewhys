"""可注入故障的模拟系统。

为什么不用真实系统？
  因为我们**必须掌握 ground truth**。用真系统就没法知道「正确答案」是什么，
  没有正确答案就没有自动判分，整个项目就退化成 demo。

两层组织：

- :class:`MockService` —— 单个服务，只会产生日志
- :class:`MockSystem`  —— 一组互相调用的服务 + 拓扑，生成跨服务的请求日志

M1 只用前者（一个服务）。M2 起用后者（三个服务互相调用）。
"""

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.service import MockService
from fivewhys.mock.topology import DEFAULT_TOPOLOGY, MockSystem, ServiceSpec

__all__ = [
    "DEFAULT_TOPOLOGY",
    "LogStore",
    "MockService",
    "MockSystem",
    "ServiceSpec",
]
