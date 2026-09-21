"""注入器的共享基础设施。

写第五种故障时你会发现：每种故障的前半段都一样 —— 收集事件、排序、写入、
顺便把指标同步记上。真正不同的只有「中间描述现象的那几十行」。

这一层就是把那前半段抽出来。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fivewhys.mock.changes import ConfigStore
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.service import MockService
from fivewhys.models import LogLevel

if TYPE_CHECKING:
    # 只在类型检查时导入，避免 mock 包内部的循环依赖
    from fivewhys.mock.topology import MockSystem

# 故障期间系统级背景流量的默认间隔（秒）。
#
# ⚠️ 这个间隔指的是**整个系统每 3 秒完成一次完整请求**（一次请求 = 三个服务各若干行日志），
# 不是「每个服务每 3 秒一条」。两者差了好几倍。
DEFAULT_TRAFFIC_INTERVAL_S = 3.0


@dataclass
class FaultScript:
    """一次故障注入的「剧本」。

    注入器只负责描述**会发生什么**；排序和写入由这里统一处理。

    为什么要先收集再排序：故障现象和背景噪声是交错生成的，
    只有排序才能保证写出去的时间顺序正确。

    ## ⚠️ 不要直接调 ``service.emit``

    请一律用这里的 :meth:`error` / :meth:`warn` / :meth:`info` / :meth:`ok`。
    它们会**同时**把事件写进日志和指标两个仓库。

    这是「日志与指标同源」的落地。各写各的话，会出现
    「日志说错误率 15%、指标说 2%」这种矛盾 —— agent 会被带偏，
    它会认为「监控没报警，问题不大」。

    ## ⚠️ 背景流量要用 :meth:`system_traffic`，不要用 :meth:`background_traffic`

    原因见 :meth:`system_traffic` 的说明 —— 一句话：
    只给被点名的服务发流量，等于告诉 agent「哪些服务没被点名」。
    """

    service: MockService
    metrics: MetricStore | None = None
    system: MockSystem | None = None

    _events: list[tuple[datetime, LogLevel, str, str | None]] = field(default_factory=list)
    _failures: list[tuple[datetime, int, int]] = field(default_factory=list)
    _successes: list[tuple[datetime, int]] = field(default_factory=list)

    # ---- 记录事件（还没写出去）----

    def log(
        self,
        ts: datetime,
        level: LogLevel,
        message: str,
        trace_id: str | None = None,
    ) -> None:
        self._events.append((ts, level, message, trace_id))

    def info(self, ts: datetime, message: str, trace_id: str | None = None) -> None:
        self.log(ts, LogLevel.INFO, message, trace_id)

    def warn(self, ts: datetime, message: str, trace_id: str | None = None) -> None:
        self.log(ts, LogLevel.WARN, message, trace_id)

    def error(
        self,
        ts: datetime,
        message: str,
        trace_id: str | None = None,
        *,
        latency_ms: int = 3000,
        status: int = 504,
    ) -> None:
        """一条 ERROR 日志 **+ 一条对应的失败采样**。两者一一对应。

        为什么不分开写：分开写就意味着「日志和指标可能对不上」，
        而那正是要防的事情。
        """
        self.log(ts, LogLevel.ERROR, message, trace_id)
        self._failures.append((ts, latency_ms, status))

    def ok(
        self,
        ts: datetime,
        message: str,
        latency_ms: int,
        trace_id: str | None = None,
    ) -> None:
        """一条正常请求日志 **+ 一条成功采样**。"""
        self.log(ts, LogLevel.INFO, message, trace_id)
        self._successes.append((ts, latency_ms))

    # ---- 常用套路 ----

    def background_traffic(
        self,
        start: datetime,
        end: datetime,
        *,
        rng: random.Random,
        path: str = "/api/v1/orders",
        interval_s: float = 3.0,
        method: str = "GET",
    ) -> None:
        """**单服务**的正常请求流量。

        ⚠️ 只有拿不到 ``system`` 时（M1 的最小单服务场景）才该用它。
        多服务场景请用 :meth:`system_traffic`。

        **不能省掉这一段。** 没有它，故障窗口里 100% 都是 ERROR/WARN，
        agent 一眼就锁定了，根本不需要推理能力。
        """
        cursor = start
        step = timedelta(seconds=interval_s)
        while cursor < end:
            base = self.service.base_latency_ms
            latency = max(1, int(rng.gauss(base, base * 0.2)))
            self.ok(
                cursor,
                f"{method} {path}/{rng.randint(100000, 999999)} 200 {latency}ms",
                latency,
                trace_id=self.service.new_trace_id(),
            )
            cursor += step

    def system_traffic(
        self,
        start: datetime,
        end: datetime,
        *,
        rng: random.Random,
        interval_s: float = DEFAULT_TRAFFIC_INTERVAL_S,
    ) -> None:
        """故障期间的正常流量 —— **走完整调用链**。

        拿不到 ``system`` 时自动退化成单服务的 :meth:`background_traffic`。

        ## 为什么必须跨服务（FIV-D1）

        背景流量不只是「制造噪声」，它还决定了**哪些服务在故障窗口里有采样**。

        只给被点名的服务发流量，会同时造成两个后果：

        1. **泄漏**：没被点名的服务在故障窗口里一条采样都没有，而健康场景
           （:mod:`fivewhys.mock.injectors.healthy` 用的是 ``system.emit_request``）
           每个服务都有。于是 agent 只要查一次下游服务的指标，
           就能判断「有没有故障」—— 完全不需要推理。
        2. **抽掉关键手法**：``dependency_5xx`` 场景就是要 agent 顺着调用链
           往下游追，可下游在故障窗口里查不到任何流量，这条路走不通。

        这个缺陷是 FIV-13 手工检查工具输出时发现的（看板上的 FIV-D1），
        当时的数据：

        ::

            故障窗口 14:02:00 ~ 14:07:00
            order-service      故障期 5 桶
            payment-service    故障期 0 桶
            inventory-service  故障期 0 桶

        ``rng`` 目前只用于退化路径；跨服务流量用系统自己的随机源 ——
        这样同一次请求在各个服务里的数字才自洽（见 ``MockSystem._handle``）。
        """
        if self.system is None:
            self.background_traffic(start, end, rng=rng, interval_s=interval_s)
            return

        cursor = start
        step = timedelta(seconds=interval_s)
        while cursor < end:
            cursor = self.system.emit_request(cursor)
            cursor += step

    # ---- 统一写入 ----

    def flush(self) -> None:
        """排序后写入日志，并把采样写进指标。

        **写完就清空缓冲区**，所以可以安全地重复调用：
        第二次 flush 只会写出「上次 flush 之后新加的事件」，不会重复写。
        """
        self._events.sort(key=lambda event: event[0])
        for ts, level, message, trace_id in self._events:
            self.service.emit(level, message, ts, trace_id=trace_id)

        if self.metrics is not None:
            for ts, latency, status in self._failures:
                self.metrics.record_request(ts, self.service.name, latency, status=status)
            for ts, latency in self._successes:
                self.metrics.record_request(ts, self.service.name, latency, status=200)

        self._events.clear()
        self._failures.clear()
        self._successes.clear()


def record_config_change(
    configs: ConfigStore | None,
    *,
    service_name: str,
    changes: dict[str, object],
    at: datetime,
    note: str,
) -> None:
    """改掉若干配置项，并记一条变更快照。

    大多数故障的根因都是「某个配置被改了」。抽出来省得每个注入器都写一遍
    「取最新快照 → 复制 → 改值 → 记录」。

    ``configs`` 为 None 时什么都不做 —— 这让注入器在不需要配置的场景里
    （比如单服务测试）也能照常工作。
    """
    if configs is None:
        return
    current = configs.latest(service_name, at)
    values = dict(current.values) if current else {}
    values.update(changes)
    configs.record_values(at, service_name, values, note=note)


__all__ = ["DEFAULT_TRAFFIC_INTERVAL_S", "FaultScript", "record_config_change"]
