"""故障注入 —— 本项目的杀手锏。

==============================================================================
TODO(M1-2)  实现 db_pool_exhausted 这一种故障
==============================================================================

## 设计铁律（这一条决定了整个项目成不成立）

注入的故障**只能通过「现象」被观察到，绝不能把答案写进日志**。

反例（错误的做法）：
    ERROR database connection pool exhausted (max=5)

这样写，agent 只要 grep 一下 "pool" 就完事了，项目毫无难度，
面试官也会当场质疑「这不就是把答案喂给模型吗」。

正例（正确的做法）：
    INFO  config reloaded from /etc/order-service/app.yaml
    ERROR order lookup failed: context deadline exceeded
    WARN  connection wait time 2991ms exceeds threshold 100ms

"context deadline exceeded" 是模糊的（可能是网络、可能是下游、可能是 DB），
agent 必须结合「错误开始的时间点」和「connection wait time 飙升」才能
推断出是连接池被耗尽。这才是推理。

## 你需要在 `inject_db_pool_exhausted` 里做的事

按时间顺序，用 `service.emit(...)` 写入下面四类日志。注意时间要错开，
让「配置变更」严格早于「错误开始」：

  1. 配置重载（INFO）—— 这是真正的触发点，出现在 at 之前一点点
       at - 2s : "config reloaded from /etc/order-service/app.yaml"

  2. 错误开始出现（ERROR），从 at 起持续约 5 分钟，逐渐变密
       每一分钟生成 2~6 条：
       "order lookup failed: context deadline exceeded"
       每条带一个独立的 trace_id

  3. 连接等待时间飙升（WARN）—— 这是指向连接池的关键线索
       紧跟在部分 ERROR 之后（同一时间戳或 +1s）：
       "connection wait time {wait_ms}ms exceeds threshold 100ms"
       wait_ms 在 2500~3100 之间随机（用 service._rng，保证可复现）

  4. 请求延迟超 SLO（WARN），稀疏出现：
       "request latency {latency}ms exceeds SLO 500ms"
       latency 在 3000~5000 之间

## 返回值

必须返回一个填好的 `GroundTruth`：

    GroundTruth(
        scenario_id=f"{service_name}-db-pool-{at:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        root_cause_service=service_name,
        root_cause="order-service 的数据库连接池上限被配置变更下调，导致连接耗尽",
        injected_at=at,
        symptoms=["错误率飙升", "请求延迟超 SLO", "connection wait time 飙升"],
        match_keywords=["connection", "pool", "连接池", "耗尽", "exhaust"],
    )

## 自测

实现完后跑：
    python -c "
    from datetime import UTC, datetime, timedelta
    from fivewhys.mock.logstore import LogStore
    from fivewhys.mock.service import MockService
    from fivewhys.mock.scenarios import inject_db_pool_exhausted
    from fivewhys.models import LogLevel

    store = LogStore()
    svc = MockService('order-service', store)
    t0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    svc.normal_operation(t0, t0 + timedelta(minutes=30))
    gt = inject_db_pool_exhausted(store, svc, t0 + timedelta(minutes=30))
    print(gt.model_dump_json(indent=2))
    print(store.stats())
    "

检查点：
  - store.stats() 里 ERROR 数量应该在 10~30 之间
  - 日志里**不能**出现 "pool" / "连接池" 字样
  - 时间戳必须严格递增
"""

from __future__ import annotations

from datetime import datetime

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.service import MockService
from fivewhys.models import GroundTruth


def inject_db_pool_exhausted(
    store: LogStore,
    service: MockService,
    at: datetime,
) -> GroundTruth:
    """注入「数据库连接池耗尽」故障。

    Args:
        store:   日志仓库
        service: 被注入故障的服务
        at:      故障开始的时间点

    Returns:
        这个场景的 ground truth。
    """
    # TODO(M1-2)：按上面「你需要在 inject_db_pool_exhausted 里做的事」实现。
    raise NotImplementedError("TODO(M1-2)：实现 db_pool_exhausted 故障注入")


__all__ = ["inject_db_pool_exhausted"]
