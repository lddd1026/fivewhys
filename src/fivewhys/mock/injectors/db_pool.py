"""连接池耗尽 —— 第一种故障，也是最好讲的一种。

## 证据链

::

    14:29:58  INFO   config reloaded from /etc/order-service/app.yaml    <- 触发点
    14:30:00  ERROR  order lookup failed: context deadline exceeded      <- 主症状
    14:30:17  WARN   connection wait time 2930ms exceeds threshold 100ms <- 关键线索
    14:30:16  WARN   request latency 3090ms exceeds SLO 500ms            <- 业务影响

    配置历史  14:29:58  db.pool_size: 50 -> 5                             <- 根因

## 为什么这条链是有效的

``context deadline exceeded`` 本身是模糊的 —— 可能是网络、可能是下游、
也可能是数据库。agent 必须：

1. 注意到错误从 14:30 开始 —— 而 14:29:58 有一次配置变更
2. 注意到 ``connection wait time`` 飙升 —— 把「连接」从其它可能里筛出来
3. 去查配置历史 —— 发现 ``pool_size`` 被从 50 改成了 5

**第 3 步是关键。** 前两步只能让它怀疑到「连接」，只有第 3 步能给出确证。

## 两个入口

- :func:`inject_db_pool` —— 核心逻辑，参数是拆开的基本件
- :func:`inject` —— 注册表用的适配层，参数是 :class:`InjectionContext`

分开是为了让老的单服务用法（``inject_db_pool_exhausted``）也能共用同一份实现，
而不是复制一遍。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from fivewhys.mock.changes import ConfigStore
from fivewhys.mock.injectors import InjectionContext, register
from fivewhys.mock.injectors._base import FaultScript
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.service import MockService
from fivewhys.models import FaultCategory, GroundTruth

# 故障持续时间
DURATION = timedelta(minutes=5)

# 故障期间背景噪声的间隔（秒）。
#
# ⚠️ 它对应的吞吐（约 0.33 QPS）明显低于正常时期的 2 QPS —— **这是有意的**。
#
# 真实故障中吞吐通常会下降：请求排队超时、客户端放弃重试、负载均衡把节点摘掉。
# 于是指标上会同时出现三个信号：
#
#   错误率上升   <- 指向「出问题了」
#   P95 飙升     <- 指向「问题有多严重」
#   QPS 下降     <- 指向「影响面有多大」
#
# **三个都不指向根因。** 指标负责「发现异常」，查明原因还得靠日志和配置。
NOISE_INTERVAL_S = 3.0

# 触发故障的配置变更内容
HEALTHY_POOL_SIZE = 50
BROKEN_POOL_SIZE = 5
CONFIG_RELOAD_NOTE = "config reloaded from /etc/order-service/app.yaml"

# 超时类失败的耗时约等于 deadline（配置值），所以用 3 秒附近的值
_TIMEOUT_MS = (2900, 3200)


def inject_db_pool(
    *,
    service: MockService,
    at: datetime,
    rng: random.Random,
    metrics: MetricStore | None = None,
    configs: ConfigStore | None = None,
) -> GroundTruth:
    """核心逻辑。两种入口都调它。"""
    script = FaultScript(service=service, metrics=metrics)

    start = at
    end = start + DURATION
    triggered_at = start - timedelta(seconds=2)

    # ---- 根因：配置变更 ----
    # 这条日志只是「触发点」—— 它不说改了什么。
    # 真正的内容在配置历史里，agent 得自己去查。
    script.info(triggered_at, CONFIG_RELOAD_NOTE)
    if configs is not None:
        current = configs.latest(service.name, start)
        values = dict(current.values) if current else {}
        values["db.pool_size"] = BROKEN_POOL_SIZE
        configs.record_values(
            triggered_at,
            service.name,
            values,
            note=CONFIG_RELOAD_NOTE,
        )

    # ---- 故障期间的现象 ----
    cursor = start
    while cursor < end:
        trace = service.new_trace_id()

        # 主症状。故意模糊 —— 可能是网络、可能是下游、也可能是 DB。
        script.error(
            cursor,
            "order lookup failed: context deadline exceeded",
            trace,
            latency_ms=rng.randint(*_TIMEOUT_MS),
        )
        cursor += timedelta(seconds=rng.randint(5, 25))

        # 关键线索。只描述「等连接变慢了」，绝不说「池满了」。
        if rng.random() < 0.8:
            wait_ms = rng.randint(2500, 3100)
            script.warn(
                cursor,
                f"connection wait time {wait_ms}ms exceeds threshold 100ms",
                trace,
            )
            cursor += timedelta(seconds=rng.randint(2, 10))

        # 业务影响。稀疏出现。
        if rng.random() < 0.3:
            latency_ms = rng.randint(3000, 5000)
            script.warn(
                cursor,
                f"request latency {latency_ms}ms exceeds SLO 500ms",
            )
            cursor += timedelta(seconds=rng.randint(2, 8))

    # ---- 背景噪声：故障期间正常请求照常进来 ----
    script.background_traffic(start, end, rng=rng, interval_s=NOISE_INTERVAL_S)
    script.flush()

    return GroundTruth(
        scenario_id=f"{service.name}-db-pool-{start:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        root_cause_service=service.name,
        root_cause=(
            f"{service.name} 的数据库连接池上限被配置变更下调"
            f"（{HEALTHY_POOL_SIZE} -> {BROKEN_POOL_SIZE}），"
            "并发请求排队等待连接，导致大面积超时"
        ),
        injected_at=start,
        symptoms=[
            "order lookup 大量 context deadline exceeded",
            "connection wait time 从 ~0ms 飙升到 3000ms",
            "请求延迟超过 500ms 的 SLO",
            "错误率从 0% 升到 10% 以上，P95 从 ~100ms 冲到 3000ms",
            "QPS 从 ~1.7 降到 ~0.4（吞吐下降）",
        ],
        match_keywords=["connection", "pool", "连接池", "耗尽", "exhaust"],
        # ⚠️ 只放真正的答案词。绝不能放 "connection" ——
        # 它是日志里的关键线索（connection wait time 飙升），必须出现。
        answer_keywords=["pool", "连接池", "耗尽", "exhaust", "连接数上限"],
    )


@register(
    FaultCategory.DB_POOL_EXHAUSTED,
    "db-pool-exhausted",
    "数据库连接池上限被配置下调，并发请求排队等连接直到超时",
)
def inject(ctx: InjectionContext) -> GroundTruth:
    """注册表入口。"""
    return inject_db_pool(
        service=ctx.service,
        at=ctx.at,
        rng=ctx.rng,
        metrics=ctx.metrics,
        configs=ctx.configs,
    )


__all__ = [
    "BROKEN_POOL_SIZE",
    "CONFIG_RELOAD_NOTE",
    "DURATION",
    "HEALTHY_POOL_SIZE",
    "NOISE_INTERVAL_S",
    "inject",
    "inject_db_pool",
]
