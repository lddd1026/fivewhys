"""``query_metrics`` —— 让 agent 看见「影响有多大」的工具。

## 它在排障路径上的位置

::

    query_metrics   错误率从 0% 涨到 15%、P95 从 45ms 冲到 3000ms
      -> query_logs 14:30 开始报 deadline exceeded
        -> get_config pool_size 50 -> 5          <- 根因
          -> get_deploy_history 排除「是发布引起的」

指标是**第一步**：它告诉你「有没有问题、从什么时候开始」，
但没有告诉你「为什么」。所以工具描述里明确教了顺序。

## 为什么返回「时间桶」而不是原始采样

真实监控系统存的就是每分钟的错误率/P95，不存每一次请求。按桶返回：
- 贴近真实（面试官问「你这不是编的吧」，答案是「这就是 Prometheus 的形态」）
- 让趋势**看得见** —— agent 要判断的是「什么时候开始变坏的」

## 一个刻意的设计：不补空桶

:meth:`MetricStore.query` 只返回**有采样的桶**。真实监控系统通常补 0，
这里不补 —— 让 agent 自己判断「这个桶一条采样都没有」意味着什么。
如果工具替它补 0，就等于替它做了一个判断。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from fivewhys.mock.metrics import DEFAULT_BUCKET_SECONDS, MetricStore
from fivewhys.tools import Tool
from fivewhys.tools._render import (
    TRUNCATED_NOTE,
    blank_result,
    fit_lines,
    render_block,
    unknown_service,
)

# 一次最多返回多少个时间桶。1 小时 = 60 个桶（按 1 分钟粒度）。
# 超过这个量，趋势反而看不出来 —— 模型需要的是「形状」，不是每一行。
MAX_BUCKETS = 120


class QueryMetricsArgs(BaseModel):
    """query_metrics 的参数。字段 description 会直接给模型看。"""

    service: str = Field(description="服务名，例如 order-service")
    start: datetime = Field(description="时间窗口起点（ISO 8601）")
    end: datetime = Field(description="时间窗口终点（ISO 8601）")
    bucket_seconds: int = Field(
        default=DEFAULT_BUCKET_SECONDS,
        ge=10,
        le=600,
        description=(
            "聚合粒度（秒）。默认 60（一分钟一个桶）。"
            "想看长时段的整体形状就用大一点，想精确定位变化的时间点就用小一点"
        ),
    )


def build_query_metrics_tool(metrics: MetricStore, *, services: Sequence[str] = ()) -> Tool:
    """把 MetricStore 绑进工具里。每个场景一个独立的 store。"""

    def _query(service: str, start: datetime, end: datetime, bucket_seconds: int) -> str:
        if end < start:
            return (
                f"参数有误：end（{end:%H:%M:%S}）早于 start（{start:%H:%M:%S}）。"
                "请给出正确的时间窗口。"
            )

        known = services or metrics.services()
        if known and service not in known:
            return unknown_service(service, known)

        buckets = metrics.query(service, start, end, bucket_seconds=bucket_seconds)
        window = f"{start:%H:%M:%S}~{end:%H:%M:%S}"
        meta = f"服务={service}  时间={window}  粒度={bucket_seconds}s"

        if not buckets:
            return blank_result(
                f"共 0 个时间桶：{service} 在 {window} 之间没有任何请求采样",
                clue=(
                    "服务名是对的，所以这只说明**时间窗口不对** —— "
                    "要么这段窗口本来就没有流量，要么故障发生在别的时间。"
                    "把窗口放宽再看一次。"
                ),
                meta=meta,
            )

        lines, truncated = fit_lines(
            (
                f"{bucket.start:%H:%M:%S}  请求 {bucket.requests:>4}  "
                f"错误 {bucket.errors:>3} ({bucket.error_rate:>6.2%})  "
                f"P50 {bucket.p50_latency_ms:>5}ms  P95 {bucket.p95_latency_ms:>6}ms  "
                f"QPS {bucket.qps:>5.2f}"
                for bucket in buckets
            ),
            limit=MAX_BUCKETS,
        )

        # 摘要给的是「形状」，不是「每一行」：错误率峰值 + P95 峰值 + 第一个出错的桶。
        # 这正是人要花力气从表格里看出来的东西 —— 工具应该替模型先算一遍。
        worst = max(buckets, key=lambda bucket: bucket.error_rate)
        slowest = max(buckets, key=lambda bucket: bucket.p95_latency_ms)
        first_bad = next((bucket for bucket in buckets if bucket.errors > 0), None)

        summary = f"共 {len(buckets)} 个时间桶，显示 {len(lines)} 个"
        if first_bad is None:
            summary += "；全部时间桶都没有失败请求（错误率 0%）"
        else:
            summary += (
                f"；最早出现错误的时间桶是 {first_bad.start:%H:%M:%S}"
                f"（错误率 {first_bad.error_rate:.1%}），"
                f"错误率最高 {worst.error_rate:.1%} @ {worst.start:%H:%M:%S}，"
                f"P95 最高 {slowest.p95_latency_ms}ms @ {slowest.start:%H:%M:%S}"
            )

        return render_block(
            summary,
            note=TRUNCATED_NOTE if truncated else None,
            meta=meta,
            lines=lines,
        )

    return Tool(
        name="query_metrics",
        description=(
            "查询某个服务在指定时间窗口内的指标（请求量 / 错误率 / P50 / P95 / QPS），"
            "按时间桶聚合。**排障的第一步通常就是它**："
            "先确认错误率和延迟从哪个时间点开始变坏，再拿这个时间点去查日志和配置。"
            "返回的摘要里已经给出了错误率峰值、P95 峰值和最早出错的时间桶。"
            "如果某个时间桶完全没有采样，说明那段时间没有流量 —— 这本身是线索。"
        ),
        args_model=QueryMetricsArgs,
        func=_query,
    )


__all__ = ["MAX_BUCKETS", "QueryMetricsArgs", "build_query_metrics_tool"]
