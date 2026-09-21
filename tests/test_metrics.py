"""FIV-7 验收测试：指标数据生成。

对应需求：FR-1（模拟系统，M2 阶段：指标）

除了聚合逻辑本身，重点验证一条设计约束：
**指标和日志必须同源。** 日志说错误率 15%、指标却说 2%，
agent 会被彻底带偏 —— 它会认为「监控没报警，问题不大」。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from fivewhys.mock import LogStore, MetricStore, MockSystem, RequestSample
from fivewhys.mock.metrics import MetricBucket
from fivewhys.mock.scenarios import inject_db_pool_exhausted

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


def _store() -> MetricStore:
    return MetricStore()


# --------------------------------------------------------------------------
# 采样
# --------------------------------------------------------------------------


def test_5xx_counts_as_failed() -> None:
    assert RequestSample(T0, "svc", 10, 500).failed
    assert RequestSample(T0, "svc", 10, 504).failed
    assert not RequestSample(T0, "svc", 10, 200).failed
    assert not RequestSample(T0, "svc", 10, 404).failed, "4xx 是客户端错误，不算服务故障"


def test_record_and_len() -> None:
    store = _store()
    store.record_request(T0, "order-service", 42)
    store.record_request(T0, "order-service", 50, status=500)

    assert len(store) == 2
    assert store.services() == ["order-service"]


def test_clear() -> None:
    store = _store()
    store.record_request(T0, "svc", 10)
    store.clear()
    assert len(store) == 0


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------


def test_qps_is_requests_per_second() -> None:
    store = _store()
    for i in range(120):  # 0.5 秒一次，共 120 次 -> 60 秒内 120 次
        store.record_request(T0 + timedelta(seconds=i * 0.5), "svc", 10)

    # 窗口要覆盖到最后一个采样（T0+59.5s）
    buckets = store.query("svc", T0, T0 + timedelta(seconds=60))
    assert len(buckets) == 1
    assert buckets[0].requests == 120
    assert buckets[0].qps == 2.0


def test_error_rate() -> None:
    store = _store()
    for _ in range(8):
        store.record_request(T0, "svc", 10, status=200)
    for _ in range(2):
        store.record_request(T0, "svc", 10, status=500)

    bucket = store.query("svc", T0, T0)[0]
    assert bucket.errors == 2
    assert bucket.error_rate == 0.2
    assert not bucket.healthy


def test_p95_uses_nearest_rank() -> None:
    """P95 = 第 95% 个请求的耗时，是一个真实存在的请求，不是插值算出来的。"""
    store = _store()
    for latency in range(1, 101):  # 1..100
        store.record_request(T0, "svc", latency)

    bucket = store.query("svc", T0, T0)[0]
    assert bucket.p50_latency_ms == 50
    assert bucket.p95_latency_ms == 95
    assert bucket.max_latency_ms == 100


def test_p95_of_single_sample_is_that_sample() -> None:
    store = _store()
    store.record_request(T0, "svc", 777)
    bucket = store.query("svc", T0, T0)[0]
    assert bucket.p95_latency_ms == 777


def test_aggregation_splits_into_minute_buckets() -> None:
    store = _store()
    store.record_request(T0, "svc", 10)
    store.record_request(T0 + timedelta(seconds=30), "svc", 10)
    store.record_request(T0 + timedelta(minutes=1), "svc", 10)
    store.record_request(T0 + timedelta(minutes=2, seconds=5), "svc", 10)

    buckets = store.query("svc", T0, T0 + timedelta(minutes=3))

    assert len(buckets) == 3
    assert [b.requests for b in buckets] == [2, 1, 1]
    assert buckets[0].start == T0
    assert buckets[1].start == T0 + timedelta(minutes=1)


def test_custom_bucket_size() -> None:
    store = _store()
    for i in range(12):  # 每 5 秒一次 -> 0,5,...,55 秒
        store.record_request(T0 + timedelta(seconds=i * 5), "svc", 10)

    buckets = store.query("svc", T0, T0 + timedelta(seconds=59), bucket_seconds=10)

    assert len(buckets) == 6  # 0s,10s,20s,30s,40s,50s —— 60s 那个桶是空的，不返回
    assert all(b.requests == 2 for b in buckets)


# --------------------------------------------------------------------------
# 过滤与边界
# --------------------------------------------------------------------------


def test_query_filters_by_service() -> None:
    store = _store()
    store.record_request(T0, "order-service", 10)
    store.record_request(T0, "payment-service", 20)

    buckets = store.query("payment-service", T0, T0)
    assert len(buckets) == 1
    assert buckets[0].service == "payment-service"
    assert buckets[0].requests == 1


def test_query_filters_by_time_window() -> None:
    store = _store()
    store.record_request(T0, "svc", 10)
    store.record_request(T0 + timedelta(hours=1), "svc", 10)

    assert len(store.query("svc", T0, T0 + timedelta(minutes=1))) == 1


def test_empty_result_returns_empty_list() -> None:
    """查不到就给空列表，不补零 —— 让 agent 自己判断这意味着什么。"""
    assert _store().query("svc", T0, T0 + timedelta(minutes=1)) == []


def test_reversed_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="end 不能早于 start"):
        _store().query("svc", T0 + timedelta(minutes=1), T0)


def test_invalid_bucket_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="bucket_seconds"):
        _store().query("svc", T0, T0, bucket_seconds=0)


# --------------------------------------------------------------------------
# 持久化（场景包 FIV-9 会用到）
# --------------------------------------------------------------------------


def test_jsonl_round_trip(tmp_path: object) -> None:
    store = _store()
    store.record_request(T0, "order-service", 42, status=200)
    store.record_request(T0 + timedelta(seconds=1), "payment-service", 99, status=504)

    path = store.dump_jsonl(tmp_path / "metrics.jsonl")  # type: ignore[operator]
    restored = MetricStore.load_jsonl(path)

    assert restored.all() == store.all()


# --------------------------------------------------------------------------
# ⭐ 与日志同源
# --------------------------------------------------------------------------


def _completion_lines(store: LogStore, service: str) -> int:
    """数某个服务发了多少条「请求完成」日志。"""
    pattern = re.compile(r"^POST .+ \d{3} \d+ms$")
    return sum(
        1
        for entry in store.all()
        if entry.service == service and pattern.match(entry.message) and entry.level.value == "INFO"
    )


def test_metric_samples_match_completion_log_lines() -> None:
    """正常流量下，指标采样数必须等于「请求完成」日志行数。

    这就是「同源」的可验证形式：同一批请求，两个视图必须说同样的话。
    """
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    system.normal_operation(T0, T0 + timedelta(seconds=30), rps=2.0)

    for name in system.names:
        expected = _completion_lines(logs, name)
        actual = sum(1 for s in system.metrics.all() if s.service == name)
        assert actual == expected, f"{name}: 指标 {actual} 条采样，日志 {expected} 条完成行"


def test_fault_shows_up_in_metrics() -> None:
    """故障必须同时反映到指标上，否则 agent 会以为「监控没报警，问题不大」。"""
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    system.normal_operation(T0, T0 + timedelta(minutes=2), rps=2.0)

    fault_at = T0 + timedelta(minutes=2)
    inject_db_pool_exhausted(
        logs,
        system.service("order-service"),
        fault_at,
        metrics=system.metrics,
    )

    healthy = system.metrics.query("order-service", T0, T0 + timedelta(minutes=1))[0]
    faulty = system.metrics.query("order-service", fault_at, fault_at + timedelta(minutes=1))[0]

    assert healthy.error_rate == 0.0
    assert faulty.error_rate > 0.0, "故障期间错误率必须上升"
    assert faulty.healthy is False
    assert faulty.p95_latency_ms > healthy.p95_latency_ms, "故障期间 P95 必须上升"


def test_fault_does_not_make_error_rate_one_hundred_percent() -> None:
    """故障期间正常请求照常进来，所以错误率是「涨了」而不是「100%」。

    100% 会让场景变得太容易 —— agent 一眼就知道整个窗口都是坏的。
    """
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    fault_at = T0
    inject_db_pool_exhausted(
        logs, system.service("order-service"), fault_at, metrics=system.metrics
    )

    buckets = system.metrics.query("order-service", fault_at, fault_at + timedelta(minutes=1))
    assert buckets
    assert buckets[0].error_rate < 1.0, "错误率不该是 100%"


def test_fault_period_throughput_drops() -> None:
    """故障期间 QPS 下降 —— 有意建模，不是参数疏漏。

    真实故障中吞吐通常会下降：请求排队超时、客户端放弃重试、负载均衡摘节点。
    它和「错误率上升」「P95 飙升」一起构成完整的故障画像，
    但**三个都不指向根因** —— 查明原因还得靠日志和 trace。
    """
    logs = LogStore()
    system = MockSystem(logs, seed=0)
    system.normal_operation(T0, T0 + timedelta(minutes=2), rps=2.0)

    fault_at = T0 + timedelta(minutes=2)
    inject_db_pool_exhausted(
        logs, system.service("order-service"), fault_at, metrics=system.metrics
    )

    healthy = system.metrics.query("order-service", T0, T0 + timedelta(minutes=1))[0]
    faulty = system.metrics.query("order-service", fault_at, fault_at + timedelta(minutes=1))[0]

    assert faulty.qps < healthy.qps, "故障期间吞吐应该下降"


def test_injector_without_metrics_still_works() -> None:
    """不传 metrics 时行为和 M1 完全一致 —— 向后兼容。"""
    logs = LogStore()
    service = MockSystem(logs, seed=0).service("order-service")
    before = len(logs)

    truth = inject_db_pool_exhausted(logs, service, T0)

    assert truth.root_cause_service == "order-service"
    assert len(logs) > before


# --------------------------------------------------------------------------
# 可复现（NFR-1）
# --------------------------------------------------------------------------


def test_metrics_are_reproducible() -> None:
    def build() -> list[str]:
        logs = LogStore()
        system = MockSystem(logs, seed=11)
        system.normal_operation(T0, T0 + timedelta(seconds=20), rps=2.0)
        inject_db_pool_exhausted(
            logs,
            system.service("order-service"),
            T0 + timedelta(seconds=20),
            metrics=system.metrics,
        )
        buckets = system.metrics.query("order-service", T0, T0 + timedelta(minutes=2))
        return [str(b) for b in buckets]

    assert build() == build()


def test_system_accepts_external_metric_store() -> None:
    """场景包要把日志和指标装在一起，所以必须能注入外部仓库。"""
    logs = LogStore()
    shared = MetricStore()
    system = MockSystem(logs, metrics=shared, seed=0)
    system.emit_request(T0)

    assert system.metrics is shared
    assert len(shared) > 0


def test_metric_bucket_has_a_stable_shape() -> None:
    """桶的字段是给 agent 看的，改动要有意识。"""
    store = _store()
    store.record_request(T0, "svc", 10)
    bucket = store.query("svc", T0, T0)[0]

    assert isinstance(bucket, MetricBucket)
    assert bucket.start == T0
    assert bucket.service == "svc"
    # qps 有意做了四位小数四舍五入 —— 给 agent 看的数字不需要 16 位精度
    assert bucket.qps == pytest.approx(1 / 60, abs=1e-4)
