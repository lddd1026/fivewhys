"""`query_logs` 工具 —— M1 阶段唯一需要实现的工具。

==============================================================================
TODO(M1-3)  实现这个工具
==============================================================================

现在 `_query` 会抛 NotImplementedError。把它换成真实实现。

实现提示：
  1. `store` 已经通过闭包传进来了（看 `build_query_logs_tool`）
  2. 用 `store.all()` 拿到全部日志，然后按 service / 时间窗口 / level / keyword 过滤
  3. 返回值是**字符串**（会直接进模型的上下文），所以要：
     - 按时间排序
     - 最多返回 limit 条
     - 做一个「共 N 条命中，显示前 limit 条」的头部
     - 一行一条，格式紧凑：`14:32:07 ERROR upstream call timed out (trace=abc123)`
  4. **不要**在返回值里暴露根因。你只是把现象给 agent 看。

自测（实现完跑这个）：
  python -c "
  from datetime import UTC, datetime, timedelta
  from fivewhys.mock.logstore import LogStore
  from fivewhys.mock.service import MockService
  from fivewhys.mock.scenarios import inject_db_pool_exhausted
  from fivewhys.tools.query_logs import build_query_logs_tool

  store = LogStore()
  svc = MockService('order-service', store)
  t0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
  svc.normal_operation(t0, t0 + timedelta(minutes=30))
  inject_db_pool_exhausted(store, svc.name, t0 + timedelta(minutes=30))

  tool = build_query_logs_tool(store)
  print(tool(service='order-service',
             start=t0 + timedelta(minutes=29),
             end=t0 + timedelta(minutes=45)))
  "
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from fivewhys.mock.logstore import LogStore
from fivewhys.tools import Tool


class QueryLogsArgs(BaseModel):
    """query_logs 的参数。字段 description 会直接给模型看，措辞很重要。"""

    service: str = Field(description="服务名，例如 order-service")
    start: datetime = Field(description="时间窗口起点（ISO 8601）")
    end: datetime = Field(description="时间窗口终点（ISO 8601）")
    levels: list[str] = Field(
        default_factory=lambda: ["WARN", "ERROR"],
        description="要看的日志级别。默认只看 WARN/ERROR，避免被 INFO 淹没",
    )
    keyword: str | None = Field(
        default=None,
        description="可选的关键字过滤（不区分大小写的子串匹配），例如 timeout",
    )
    limit: int = Field(default=50, le=200, description="最多返回多少条")


def build_query_logs_tool(store: LogStore) -> Tool:
    """把 LogStore 绑进工具里。

    用闭包而不是全局变量：测试时可以给每个场景一个独立的 store，
    并发跑多个场景时不会互相污染。
    """

    def _query(
        service: str,
        start: datetime,
        end: datetime,
        levels: list[str],
        keyword: str | None,
        limit: int,
    ) -> str:
        # TODO(M1-3)：在这里实现。参考上面的实现提示。
        raise NotImplementedError("TODO(M1-3)：实现 query_logs")

    return Tool(
        name="query_logs",
        description=(
            "查询某个服务在指定时间窗口内的日志。"
            "排障的第一步通常就是它：先看有没有报错，再看报错从什么时间点开始。"
            "默认只返回 WARN/ERROR，避免被 INFO 日志淹没。"
            "如果结果为空，说明这个服务在这个时间窗口内没有异常日志 —— 这本身就是线索。"
        ),
        args_model=QueryLogsArgs,
        func=_query,
    )


__all__ = ["QueryLogsArgs", "build_query_logs_tool"]
