"""注入器的共享基础设施。

写第五种故障时你会发现：每种故障的前半段都一样 —— 收集事件、排序、写入、
顺便把指标同步记上。真正不同的只有「中间描述现象的那几十行」。

这一层就是把那前半段抽出来。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.service import MockService
from fivewhys.models import LogLevel


@dataclass
class FaultScript:
    """一次故障注入的「剧本」。

    注入器只负责描述**会发生什么**；排序和写入由这里统一处理。

    为什么要先收集再排序：故障现象和背景噪声是交错生成的，
    只有排序才能保证写出去的时间顺序正确。

    ## ⚠️ 不要直接调 ``service.emit``

    请一律用这里的 :meth:`error` / :meth:`warn` / :meth:`info` / :meth:`ok`。
    它们会**同时**把事件写进日志和指标两个仓库。

    这是「日志与指标同源」的落地点。各写各的话，会出现
    「日志说错误率 15%、指标说 2%」这种矛盾 —— agent 会被带偏，
    它会认为「监控没报警，问题不大」。
    """

    service: MockService
    metrics: MetricStore | None = None

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
        """故障期间的正常请求流量。

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


__all__ = ["FaultScript"]
