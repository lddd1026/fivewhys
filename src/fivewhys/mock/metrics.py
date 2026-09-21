"""指标 —— agent 的第二类证据。

## 日志和指标的分工

- **日志**告诉 agent「发生了什么」：哪一行报错了、什么时候开始
- **指标**告诉 agent「影响有多大」：错误率从 0% 涨到 15%、P95 延迟从 50ms 涨到 3000ms

真实排障就是这三层互相印证：
**指标发现异常 → 追踪定位范围 → 日志查明原因**（见 README 里的三大支柱）。

## 一个关键设计：指标和日志必须同源

指标不是"另外编一套数字"，而是从**同一批请求采样**里聚合出来的。

为什么较真这一点：如果日志说错误率 15%、指标却说 2%，agent 会被彻底带偏 ——
它会认为"指标正常，所以问题不大"，从而走向错误结论。

这也正是 FIV-6 踩过的坑：耗时各写各的随机数，导致「下游自称 30ms、上游看到 5ms」。
**同一份事实，只能有一个来源。**

## 为什么按时间桶聚合

真实监控系统不存每一次请求，它存的是「每分钟的错误率、P95」。
按桶聚合既贴近真实，也让 agent 面对的是可读的数字而不是几万行原始采样。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# 默认聚合粒度：1 分钟。和真实监控系统一致。
DEFAULT_BUCKET_SECONDS = 60


@dataclass(frozen=True)
class RequestSample:
    """一次已完成请求的采样 —— 指标的唯一数据源。"""

    ts: datetime
    service: str
    latency_ms: int
    status: int

    @property
    def failed(self) -> bool:
        """5xx 及以上算失败。"""
        return self.status >= 500


@dataclass(frozen=True)
class MetricBucket:
    """一个时间桶内的聚合结果。"""

    start: datetime
    service: str
    requests: int
    errors: int
    qps: float
    error_rate: float
    p50_latency_ms: int
    p95_latency_ms: int
    max_latency_ms: int

    @property
    def healthy(self) -> bool:
        """没有失败请求就算健康。"""
        return self.errors == 0


class MetricStore:
    """按服务 + 时间桶聚合的指标仓库。"""

    def __init__(self) -> None:
        self._samples: list[RequestSample] = []

    # ---- 写入 ----

    def record(self, sample: RequestSample) -> None:
        self._samples.append(sample)

    def record_request(
        self,
        ts: datetime,
        service: str,
        latency_ms: int,
        status: int = 200,
    ) -> RequestSample:
        """便捷写法，省得每次都构造 ``RequestSample``。"""
        sample = RequestSample(ts=ts, service=service, latency_ms=latency_ms, status=status)
        self._samples.append(sample)
        return sample

    def extend(self, samples: list[RequestSample]) -> None:
        self._samples.extend(samples)

    def clear(self) -> None:
        self._samples.clear()

    # ---- 读取 ----

    def all(self) -> list[RequestSample]:
        return list(self._samples)

    def __len__(self) -> int:
        return len(self._samples)

    def services(self) -> list[str]:
        return sorted({sample.service for sample in self._samples})

    def query(
        self,
        service: str,
        start: datetime,
        end: datetime,
        *,
        bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    ) -> list[MetricBucket]:
        """按时间桶聚合 ``[start, end]`` 区间内的指标。

        只返回**有采样的桶**。真实监控系统里空桶通常补 0，但这里不补 ——
        让 agent 自己判断「这个桶一条采样都没有」意味着什么。
        """
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds 必须为正")
        if end < start:
            raise ValueError("end 不能早于 start")

        buckets: dict[datetime, list[RequestSample]] = {}
        for sample in self._samples:
            if sample.service != service or not (start <= sample.ts <= end):
                continue
            key = _bucket_start(sample.ts, bucket_seconds)
            buckets.setdefault(key, []).append(sample)

        return [
            _aggregate(key, service, samples, bucket_seconds)
            for key, samples in sorted(buckets.items())
        ]

    # ---- 持久化（场景包要用，见 FIV-9）----

    def to_jsonl(self) -> str:
        """整个仓库序列化成 JSONL 文本。落盘与快照指纹共用这一份（见 FIV-12）。"""
        return "".join(sample_to_json(sample) + "\n" for sample in self._samples)

    def dump_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8", newline="\n")
        return path

    @classmethod
    def load_jsonl(cls, path: Path) -> MetricStore:
        store = cls()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    store.record(sample_from_json(line))
        return store


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _bucket_start(ts: datetime, bucket_seconds: int) -> datetime:
    """把时间戳对齐到桶的起点（向下取整）。"""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % bucket_seconds, tz=UTC)


def _percentile(values: list[int], percentile: float) -> int:
    """最近秩法（nearest-rank）求分位数。

    为什么不用插值法：分位数在监控里的约定就是「P95 = 第 95% 个请求的耗时」，
    是个真实存在的请求，不是一个算出来的虚构值。
    """
    if not values:
        return 0
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[max(0, min(rank - 1, len(ordered) - 1))]


def _aggregate(
    start: datetime,
    service: str,
    samples: list[RequestSample],
    bucket_seconds: int,
) -> MetricBucket:
    latencies = [sample.latency_ms for sample in samples]
    errors = sum(1 for sample in samples if sample.failed)

    return MetricBucket(
        start=start,
        service=service,
        requests=len(samples),
        errors=errors,
        qps=round(len(samples) / bucket_seconds, 4),
        error_rate=round(errors / len(samples), 4),
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        max_latency_ms=max(latencies),
    )


def sample_to_json(sample: RequestSample) -> str:
    return json.dumps(
        {
            "ts": sample.ts.isoformat(),
            "service": sample.service,
            "latency_ms": sample.latency_ms,
            "status": sample.status,
        },
        ensure_ascii=False,
    )


def sample_from_json(raw: str) -> RequestSample:
    data = json.loads(raw)
    return RequestSample(
        ts=datetime.fromisoformat(data["ts"]),
        service=data["service"],
        latency_ms=int(data["latency_ms"]),
        status=int(data["status"]),
    )


__all__ = [
    "DEFAULT_BUCKET_SECONDS",
    "MetricBucket",
    "MetricStore",
    "RequestSample",
    "sample_from_json",
    "sample_to_json",
]
