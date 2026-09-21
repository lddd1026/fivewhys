"""日志配置 —— 全项目只在这里配一次。

## 为什么需要它（上线前测试发现）

在这之前，**全项目没有一处配置日志**。Python 的兜底行为是把 WARNING 及以上
打到 stderr，而主循环里用的是 ``logger.exception`` —— 连 60 行堆栈一起打。

后果实测：用户填错 API Key 时，命令行会在结果表格之前甩出一大段
litellm 内部 traceback，而表格里只写「未提交结论（error）」。
既吓人，又没说清到底是 401 还是网络断了。

## 约定

- 默认 ``WARNING``：一行、无堆栈。**用户看的是结论，不是堆栈。**
- ``-v`` / ``verbose=True``：``DEBUG``，带完整堆栈 —— 排查时要有东西可看。
- litellm 自己的 INFO 横幅（"Give Feedback / Get Help"）默认压掉：
  它对调试有用，对用户是噪声。
"""

from __future__ import annotations

import logging
import sys

DEFAULT_FORMAT = "%(levelname)s %(name)s: %(message)s"

# 这些 logger 默认太吵，一并在非 verbose 模式下压掉。
NOISY_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "httpcore")


def configure_logging(*, verbose: bool = False) -> None:
    """把日志收口成「默认安静、要细节时再开」。

    Args:
        verbose: 是否输出调试细节（含异常堆栈）。
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format=DEFAULT_FORMAT,
        stream=sys.stderr,
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.CRITICAL)


__all__ = ["DEFAULT_FORMAT", "NOISY_LOGGERS", "configure_logging"]
