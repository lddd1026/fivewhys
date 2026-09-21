"""故障注入 —— 本项目的杀手锏。

## 设计铁律

注入的故障**只能通过「现象」被观察到，绝不能把答案写进日志**。

反例（错误）：:

    ERROR database connection pool exhausted (max=5)

这样写，agent 只要 grep 一下 "pool" 就完事了。项目退化成「用 LLM 做了一次
字符串搜索」，准确率接近 100%，简历上那个数字毫无意义，面试官也会当场质疑。

正例（正确）：:

    INFO  config reloaded from /etc/order-service/app.yaml
    ERROR order lookup failed: context deadline exceeded
    WARN  connection wait time 2991ms exceeds threshold 100ms

``context deadline exceeded`` 是模糊的（网络？下游？DB？），agent 必须结合
「错误开始的时间点」和「connection wait time 飙升」才能推断出连接池被耗尽。
**这才是推理，这才是项目。**

## 为什么必须自己造系统

因为要掌握 ground truth。用真实系统就不知道正确答案；没有正确答案就没有
自动判分；没有自动判分，评测无从谈起 —— 整个项目会退化成 demo。

## 场景：db_pool_exhausted

故事线::

    14:29:58  运维改了配置，连接池上限 50 -> 5
    14:30:00  并发请求拿不到连接，开始排队
    14:30:05  排队超过超时阈值，请求开始失败
    14:35:00  故障持续

证据链（agent 需要看到这些才能推出结论）：

=================  ====================================================
日志               作用
=================  ====================================================
config reloaded    触发点。解释「为什么突然开始」。没有它，agent 只能
                   看到错误，却不知道是什么变了
deadline exceeded  主症状。故意模糊，不能直接点破是连接池
connection wait    关键线索。把「连接」从网络 / 下游等可能性里筛出来
latency 超 SLO     业务影响。坐实「这是影响线上的故障」
=================  ====================================================

故障期间**正常请求照常进来**。否则 agent 一眼就看出「这个窗口里全是异常」，
根本不需要任何推理。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.service import MockService
from fivewhys.models import FaultCategory, GroundTruth, LogLevel

# 故障持续时间
_FAULT_DURATION = timedelta(minutes=5)

# 故障期间背景噪声的间隔（秒）
_NOISE_INTERVAL_S = 3


def inject_db_pool_exhausted(
    store: LogStore,
    service: MockService,
    at: datetime,
) -> GroundTruth:
    """注入「数据库连接池耗尽」故障。

    Args:
        store: 日志仓库。当前实现通过 ``service.emit()`` 写入，所以这个参数
            暂时用不到；M2/M3 会用到 —— 那时一个故障需要往多个服务写日志。
        service: 被注入故障的服务
        at: 故障开始的时间点

    Returns:
        这个场景的 ground truth，供评测判分使用。
    """
    # 用 service 自己的 rng，不要用全局 random。
    # 场景必须可复现 —— 否则无法比较「改进前后」的效果差异。
    rng = service.rng
    end = at + _FAULT_DURATION

    # 先收集成事件列表，最后统一排序写入。
    # 为什么要这样：故障现象和背景噪声是交错生成的，只有排序才能保证
    # 写出去的时间顺序正确。
    events: list[tuple[datetime, LogLevel, str, str | None]] = []

    # ---- 证据 1：触发点。严格早于故障开始 ----
    events.append(
        (
            at - timedelta(seconds=2),
            LogLevel.INFO,
            "config reloaded from /etc/order-service/app.yaml",
            None,
        )
    )

    # ---- 证据 2 / 3 / 4：故障期间的现象 ----
    cursor = at
    while cursor < end:
        trace = service.new_trace_id()

        # 证据 2：主症状。故意模糊 —— 可能是网络、可能是下游、也可能是 DB。
        events.append(
            (
                cursor,
                LogLevel.ERROR,
                "order lookup failed: context deadline exceeded",
                trace,
            )
        )
        cursor += timedelta(seconds=rng.randint(5, 25))

        # 证据 3：关键线索。只描述「等连接变慢了」，绝不说「池满了」。
        if rng.random() < 0.8:
            wait_ms = rng.randint(2500, 3100)
            events.append(
                (
                    cursor,
                    LogLevel.WARN,
                    f"connection wait time {wait_ms}ms exceeds threshold 100ms",
                    trace,
                )
            )
            cursor += timedelta(seconds=rng.randint(2, 10))

        # 证据 4：业务影响。稀疏出现。
        if rng.random() < 0.3:
            latency_ms = rng.randint(3000, 5000)
            events.append(
                (
                    cursor,
                    LogLevel.WARN,
                    f"request latency {latency_ms}ms exceeds SLO 500ms",
                    None,
                )
            )
            cursor += timedelta(seconds=rng.randint(2, 8))

    # ---- 背景噪声：故障期间正常请求照常进来 ----
    # 没有这段，故障窗口里 100% 是 ERROR/WARN，agent 一眼就能锁定，
    # 不需要任何推理能力。
    noise_cursor = at
    while noise_cursor < end:
        latency_ms = max(
            1,
            int(rng.gauss(service.base_latency_ms, service.base_latency_ms * 0.2)),
        )
        events.append(
            (
                noise_cursor,
                LogLevel.INFO,
                f"GET /api/v1/orders/{rng.randint(100000, 999999)} 200 {latency_ms}ms",
                service.new_trace_id(),
            )
        )
        noise_cursor += timedelta(seconds=_NOISE_INTERVAL_S)

    # ---- 排序后统一写入。sorted 是稳定的，同一时刻保持插入顺序 ----
    events.sort(key=lambda event: event[0])
    for ts, level, message, trace_id in events:
        service.emit(level, message, ts, trace_id=trace_id)

    return GroundTruth(
        scenario_id=f"{service.name}-db-pool-{at:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        root_cause_service=service.name,
        root_cause=(
            "order-service 的数据库连接池上限被配置变更下调（50 -> 5），"
            "并发请求排队等待连接，导致大面积超时"
        ),
        injected_at=at,
        symptoms=[
            "order lookup 大量 context deadline exceeded",
            "connection wait time 从 ~0ms 飙升到 3000ms",
            "请求延迟超过 500ms 的 SLO",
        ],
        match_keywords=["connection", "pool", "连接池", "耗尽", "exhaust"],
        # ⚠️ 只放真正的答案词。绝不能放 "connection" ——
        # 它是日志里的关键线索（connection wait time 飙升），必须出现。
        answer_keywords=["pool", "连接池", "耗尽", "exhaust", "连接数上限"],
    )


__all__ = ["inject_db_pool_exhausted"]
