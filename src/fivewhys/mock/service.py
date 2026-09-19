"""一个最小的 mock 服务 —— 只会产生日志。

M1 故意做得很糙：一个类、几个方法、没有网络、没有 FastAPI。
M2 才会扩展成 3 个互相调用的服务。

记住原则：**先竖切跑通，再横向扩复杂**。
现在丑是正常的，不要在这一步想着「设计一个通用框架」。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from fivewhys.mock.logstore import LogStore
from fivewhys.models import LogEntry, LogLevel

# 正常请求的日志模板（背景噪声）。
# 有噪声很重要 —— 如果日志里只有异常，agent 根本不需要推理。
_NORMAL_REQUEST = "GET /api/v1/orders/{order_id} 200 {latency_ms}ms"
_NORMAL_HEALTH = "healthcheck ok latency={latency_ms}ms"


class MockService:
    """一个只会写日志的假服务。"""

    def __init__(
        self,
        name: str,
        store: LogStore,
        *,
        base_latency_ms: int = 45,
        seed: int = 0,
    ) -> None:
        self.name = name
        self.store = store
        self.base_latency_ms = base_latency_ms
        # 固定种子 —— 让生成的场景可复现。这是评测的前提。
        self._rng = random.Random(seed)

    # ---- 底层写入 ----

    def emit(
        self,
        level: LogLevel,
        message: str,
        ts: datetime,
        *,
        trace_id: str | None = None,
    ) -> LogEntry:
        entry = LogEntry(
            ts=ts,
            service=self.name,
            level=level,
            message=message,
            trace_id=trace_id,
        )
        self.store.append(entry)
        return entry

    def new_trace_id(self) -> str:
        return f"{self._rng.getrandbits(48):012x}"

    @property
    def rng(self) -> random.Random:
        """暴露随机源，供故障注入器使用。

        为什么用 property 而不是让外部直接碰 ``_rng``：
        私有属性一旦被别的模块依赖，以后就改不动了。开一个明确的入口，
        既表达了「这是给你用的」，也留下了将来换实现的余地。
        """
        return self._rng

    # ---- 正常流量 ----

    def normal_operation(
        self,
        start: datetime,
        end: datetime,
        *,
        rps: float = 5.0,
    ) -> int:
        """在 [start, end) 之间生成正常的请求日志。

        返回生成的日志条数。M1 用它来制造背景噪声。
        """
        if end <= start:
            raise ValueError("end 必须晚于 start")

        interval = timedelta(seconds=1.0 / rps) if rps > 0 else timedelta(seconds=1)
        count = 0
        cursor = start
        while cursor < end:
            # 正常延迟在基准值上下抖动
            latency = max(1, int(self._rng.gauss(self.base_latency_ms, self.base_latency_ms * 0.2)))

            if self._rng.random() < 0.03:
                message = _NORMAL_HEALTH.format(latency_ms=latency)
            else:
                message = _NORMAL_REQUEST.format(
                    order_id=self._rng.randint(100000, 999999),
                    latency_ms=latency,
                )

            self.emit(
                LogLevel.INFO,
                message,
                cursor,
                trace_id=self.new_trace_id(),
            )
            count += 1
            cursor += interval

        return count


__all__ = ["MockService"]
