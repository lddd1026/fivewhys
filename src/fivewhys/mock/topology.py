"""服务拓扑 —— 谁调用谁。

## M1 和 M2 的区别

M1 只有一个服务，agent 面对的是一堆**孤立的日志**。

M2 让世界变真：三个服务互相调用，**同一次请求的日志散落在不同服务里**，
只有顺着 trace_id 才能串起来。这才是分布式排障的真实样子，
也才是 trace_id 真正发挥价值的地方 —— M1 里它只是"能追"，这里是"必须追"。

## 一次请求长什么样

::

    order-service      INFO  received POST /api/v1/orders                  trace=X
    order-service      INFO  calling payment-service POST /api/v1/charge   trace=X
    payment-service    INFO  received POST /api/v1/charge                  trace=X
    payment-service    INFO  POST /api/v1/charge 200 28ms                  trace=X
    order-service      INFO  payment-service responded 200 in 32ms         trace=X
    order-service      INFO  calling inventory-service POST /api/v1/reserve trace=X
    inventory-service  INFO  received POST /api/v1/reserve                 trace=X
    inventory-service  INFO  POST /api/v1/reserve 200 17ms                 trace=X
    order-service      INFO  inventory-service responded 200 in 21ms       trace=X
    order-service      INFO  POST /api/v1/orders 200 76ms                  trace=X

**十行日志散落在三个服务里，共享同一个 trace_id。**

而且**数字是自洽的**：上游观察到的 32ms ≥ 下游自报的 28ms（差值是网络往返），
入口服务的总耗时 ≥ 两个下游观察值之和。agent 正是靠这些数字推理的，
不自洽的数据会把它带偏。

## 为什么不用真的 HTTP

评测要跑上百次，HTTP 会让总耗时翻好几倍，而它不改变 agent 面临的任何问题。
详见 docs/DESIGN.md 的 M2 决策。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.service import MockService
from fivewhys.models import LogLevel


@dataclass(frozen=True)
class ServiceSpec:
    """一个服务在拓扑里的位置。"""

    name: str
    path: str
    base_latency_ms: int = 45
    calls: tuple[str, ...] = ()


# 默认拓扑：order 是入口，它调用 payment 和 inventory
DEFAULT_TOPOLOGY: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        name="order-service",
        path="/api/v1/orders",
        base_latency_ms=45,
        calls=("payment-service", "inventory-service"),
    ),
    ServiceSpec(name="payment-service", path="/api/v1/charge", base_latency_ms=30),
    ServiceSpec(name="inventory-service", path="/api/v1/reserve", base_latency_ms=25),
)


class MockSystem:
    """一组互相调用的服务，共享同一个日志仓库。

    为什么共享仓库：真实排障时你查的就是**一个集中的日志系统**，
    而不是挨个服务去连。共享仓库也保证了 trace_id 能跨服务关联。
    """

    def __init__(
        self,
        store: LogStore,
        *,
        specs: tuple[ServiceSpec, ...] = DEFAULT_TOPOLOGY,
        seed: int = 0,
        metrics: MetricStore | None = None,
    ) -> None:
        if not specs:
            raise ValueError("拓扑不能为空")

        self.store = store
        self.seed = seed
        # 指标仓库。可以由外部传入 —— 一个场景的日志和指标必须装在同一个
        # 场景包里（见 FIV-9），所以不能每次自己 new 一个。
        self.metrics = metrics if metrics is not None else MetricStore()
        self._specs: dict[str, ServiceSpec] = {spec.name: spec for spec in specs}
        self._rng = random.Random(seed)  # 系统级随机源：只用来生成 trace_id 和调度

        # 每个服务有自己的 rng（由系统种子派生），保证可复现
        self._services: dict[str, MockService] = {
            name: MockService(
                name,
                store,
                base_latency_ms=spec.base_latency_ms,
                seed=seed * 1000 + index,
            )
            for index, (name, spec) in enumerate(self._specs.items())
        }

        self.entry = specs[0].name

    # ---- 查询 ----

    @property
    def names(self) -> list[str]:
        return list(self._specs)

    def service(self, name: str) -> MockService:
        if name not in self._services:
            raise KeyError(f"未知服务「{name}」。可用：{sorted(self._specs)}")
        return self._services[name]

    def dependencies_of(self, name: str) -> tuple[str, ...]:
        """``name`` 直接调用了哪些下游服务。"""
        if name not in self._specs:
            raise KeyError(f"未知服务「{name}」。可用：{sorted(self._specs)}")
        return self._specs[name].calls

    def callers_of(self, name: str) -> tuple[str, ...]:
        """谁调用了 ``name`` —— 排障时反向查上游要用。"""
        return tuple(spec.name for spec in self._specs.values() if name in spec.calls)

    def describe(self) -> dict[str, list[str]]:
        """给人（和 agent）看的拓扑图。M4 的 get_dependencies 工具会用它。"""
        return {name: list(spec.calls) for name, spec in self._specs.items()}

    # ---- 生成流量 ----

    def new_trace_id(self) -> str:
        """由**系统**生成，而不是某个服务 —— 因为一个 trace 跨多个服务。"""
        return f"{self._rng.getrandbits(48):012x}"

    def emit_request(
        self,
        at: datetime,
        *,
        entry: str | None = None,
        trace: str | None = None,
    ) -> datetime:
        """生成一次完整请求的跨服务日志。返回这次请求结束的时间点。"""
        return self._handle(
            entry or self.entry,
            at,
            trace or self.new_trace_id(),
        )

    def _handle(self, name: str, at: datetime, trace: str) -> datetime:
        """递归处理一次调用，返回它结束的时间点。

        ⚠️ 所有耗时都必须是**真实的时间推进量**，不能单独编一个随机数。
        否则会出现「下游自称 30ms，上游却观察到 5ms」这种物理上不可能的数字 ——
        而 agent 恰恰要靠这些数字推理，不自洽的数据会把它带偏。
        这个 bug 是手工看日志时发现的（单测只断言了结构，没断言数字关系）。
        """
        spec = self._specs[name]
        service = self._services[name]
        rng = self._rng

        started = at
        cursor = at
        service.emit(
            LogLevel.INFO,
            f"received POST {spec.path}",
            cursor,
            trace_id=trace,
        )
        cursor += timedelta(milliseconds=rng.randint(1, 3))

        for downstream in spec.calls:
            child = self._specs[downstream]
            service.emit(
                LogLevel.INFO,
                f"calling {downstream} POST {child.path}",
                cursor,
                trace_id=trace,
            )
            cursor += timedelta(milliseconds=rng.randint(1, 3))  # 请求发出

            call_started = cursor
            cursor = self._handle(downstream, cursor, trace)
            cursor += timedelta(milliseconds=rng.randint(1, 3))  # 响应返回

            # 上游观察到的时间 = 下游自身耗时 + 往返网络开销，所以一定 ≥ 下游自报值
            observed_ms = _milliseconds_between(call_started, cursor)
            service.emit(
                LogLevel.INFO,
                f"{downstream} responded 200 in {observed_ms}ms",
                cursor,
                trace_id=trace,
            )

        # 自己的处理时间（本地逻辑 + 数据库）
        cursor += timedelta(
            milliseconds=rng.randint(max(1, spec.base_latency_ms // 2), spec.base_latency_ms)
        )

        total_ms = _milliseconds_between(started, cursor)
        service.emit(
            LogLevel.INFO,
            f"POST {spec.path} 200 {total_ms}ms",
            cursor,
            trace_id=trace,
        )
        # 指标与日志**同源**：上面这条完成日志，对应下面这一条采样。
        # 不允许在这里另编一个耗时 —— 那会让日志和指标互相矛盾。
        self.metrics.record_request(cursor, name, total_ms, status=200)
        return cursor

    def normal_operation(
        self,
        start: datetime,
        end: datetime,
        *,
        rps: float = 2.0,
    ) -> int:
        """在 ``[start, end)`` 之间持续生成正常的跨服务流量。

        返回生成的**请求数**（不是日志行数 —— 一次请求会产生多行）。
        """
        if end <= start:
            raise ValueError("end 必须晚于 start")

        interval = timedelta(seconds=1.0 / rps) if rps > 0 else timedelta(seconds=1)
        cursor = start
        requests = 0

        while cursor < end and requests < _MAX_REQUESTS_PER_CALL:
            cursor = self.emit_request(cursor)
            requests += 1
            cursor += interval

        return requests


# 一次 normal_operation 的自我保护上限。
# 没有它的话，rps 或时间窗口配错会生成上百万条日志，把内存打爆、
# 也让评测慢得没法用。宁可有上限并说出来，也不要静默卡死。
_MAX_REQUESTS_PER_CALL = 5000


def _milliseconds_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() * 1000)


__all__ = ["DEFAULT_TOPOLOGY", "MockSystem", "ServiceSpec"]
