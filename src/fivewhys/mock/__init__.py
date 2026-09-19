"""可注入故障的模拟系统。

为什么不用真实系统？
  因为我们**必须掌握 ground truth**。用真系统就没法知道「正确答案」是什么，
  没有正确答案就没有自动判分，整个项目就退化成 demo。

M1 只有一个服务、一种故障。M2 会扩到 3 个互相调用的服务 + 指标 + 配置 + 发布历史。
"""

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.service import MockService

__all__ = ["LogStore", "MockService"]
