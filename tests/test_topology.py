"""FIV-6 验收测试：三服务拓扑与跨服务调用日志。

对应需求：FR-1（模拟系统，M2 阶段：3 个服务 + 调用依赖）

这一层的价值全在**跨服务关联**上：一次请求的日志散落在三个服务里，
只有顺着 trace_id 才能串起来。所以下面的测试重点验证这一点。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from fivewhys.mock import DEFAULT_TOPOLOGY, LogStore, MockSystem
from fivewhys.models import LogLevel

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


def _system(seed: int = 0) -> tuple[MockSystem, LogStore]:
    store = LogStore()
    return MockSystem(store, seed=seed), store


# --------------------------------------------------------------------------
# 拓扑本身
# --------------------------------------------------------------------------


def test_default_topology_has_three_services() -> None:
    assert [spec.name for spec in DEFAULT_TOPOLOGY] == [
        "order-service",
        "payment-service",
        "inventory-service",
    ]


def test_entry_service_calls_two_downstreams() -> None:
    system, _ = _system()
    assert system.dependencies_of("order-service") == (
        "payment-service",
        "inventory-service",
    )
    assert system.dependencies_of("payment-service") == ()


def test_reverse_lookup_finds_callers() -> None:
    """排障时经常要反向查：这个服务的异常，是谁引起的？"""
    system, _ = _system()
    assert system.callers_of("payment-service") == ("order-service",)
    assert system.callers_of("order-service") == ()


def test_describe_returns_the_graph() -> None:
    system, _ = _system()
    assert system.describe() == {
        "order-service": ["payment-service", "inventory-service"],
        "payment-service": [],
        "inventory-service": [],
    }


def test_unknown_service_raises_with_helpful_message() -> None:
    system, _ = _system()
    with pytest.raises(KeyError, match="未知服务"):
        system.service("database-service")


def test_empty_topology_is_rejected() -> None:
    with pytest.raises(ValueError, match="拓扑不能为空"):
        MockSystem(LogStore(), specs=())


# --------------------------------------------------------------------------
# ⭐ 跨服务关联：这一层的全部意义
# --------------------------------------------------------------------------


def test_one_request_touches_all_three_services() -> None:
    system, store = _system()
    system.emit_request(T0)

    services = {entry.service for entry in store.all()}
    assert services == {"order-service", "payment-service", "inventory-service"}


def test_one_request_shares_a_single_trace_id() -> None:
    """一次请求的所有日志共享一个 trace_id —— 这是跨服务追踪的前提。"""
    system, store = _system()
    system.emit_request(T0)

    traces = {entry.trace_id for entry in store.all()}
    assert len(traces) == 1, f"一次请求应该只有一个 trace_id，实际有 {len(traces)} 个"
    assert None not in traces


def test_different_requests_get_different_trace_ids() -> None:
    system, store = _system()
    system.emit_request(T0)
    system.emit_request(T0 + timedelta(seconds=1))

    traces = {entry.trace_id for entry in store.all()}
    assert len(traces) == 2


def test_trace_can_reconstruct_the_call_path() -> None:
    """顺着一个 trace_id 能还原出完整的调用路径 —— agent 要做的就是这件事。"""
    system, store = _system()
    system.emit_request(T0)

    trace = store.all()[0].trace_id
    messages = [e.message for e in store.all() if e.trace_id == trace]

    # 入口服务接收请求
    assert "received POST /api/v1/orders" in messages
    # 它调用了两个下游
    assert "calling payment-service POST /api/v1/charge" in messages
    assert "calling inventory-service POST /api/v1/reserve" in messages
    # 下游各自记录了自己收到请求
    assert "received POST /api/v1/charge" in messages
    assert "received POST /api/v1/reserve" in messages
    # 下游返回
    assert any(m.startswith("payment-service responded 200 in") for m in messages)
    assert any(m.startswith("inventory-service responded 200 in") for m in messages)
    # 入口服务最后返回
    assert any(m.startswith("POST /api/v1/orders 200") for m in messages)


def test_logs_are_ordered_within_a_request() -> None:
    """同一次请求的日志必须按时间递增 —— 否则没法还原因果。"""
    system, store = _system()
    system.emit_request(T0)

    stamps = [entry.ts for entry in store.all()]
    assert stamps == sorted(stamps)


def test_downstream_logs_fall_between_the_call_and_its_return() -> None:
    """下游的日志必须夹在「calling」和「responded」之间，否则因果是错的。"""
    system, store = _system()
    system.emit_request(T0)
    entries = store.all()

    def index_of(predicate: object) -> int:
        return next(i for i, e in enumerate(entries) if predicate(e))  # type: ignore[operator]

    calling = index_of(lambda e: e.message.startswith("calling inventory-service"))
    downstream = index_of(lambda e: e.service == "inventory-service")
    responded = index_of(lambda e: e.message.startswith("inventory-service responded"))

    assert calling < downstream < responded


# --------------------------------------------------------------------------
# 正常流量
# --------------------------------------------------------------------------


def test_normal_operation_generates_many_requests() -> None:
    system, store = _system()
    requests = system.normal_operation(T0, T0 + timedelta(seconds=5), rps=2.0)

    assert requests >= 5
    # 一次请求会产生多行日志，所以日志数一定远多于请求数
    assert len(store) > requests * 3


def test_normal_operation_is_reproducible() -> None:
    """同一个种子跑两次必须完全一致 —— 评测的前提（NFR-1）。"""

    def build() -> list[str]:
        system, store = _system(seed=7)
        system.normal_operation(T0, T0 + timedelta(seconds=3), rps=2.0)
        return [entry.model_dump_json() for entry in store.all()]

    assert build() == build()


def test_all_normal_traffic_is_info_level() -> None:
    """正常流量不该产生任何 WARN/ERROR —— 否则「正常场景」就没法用来测误报。"""
    system, store = _system()
    system.normal_operation(T0, T0 + timedelta(seconds=5), rps=2.0)

    levels = {entry.level for entry in store.all()}
    assert levels == {LogLevel.INFO}


def test_reversed_window_is_rejected() -> None:
    system, _ = _system()
    with pytest.raises(ValueError, match="end 必须晚于 start"):
        system.normal_operation(T0, T0 - timedelta(seconds=1))


def test_normal_operation_has_an_upper_bound() -> None:
    """参数配错时不能静默生成上百万条日志把内存打爆。"""
    system, store = _system()
    # 一天 × 每秒 1000 次请求，远超上限
    system.normal_operation(T0, T0 + timedelta(days=1), rps=1000)

    assert len(store) < 100_000, "应该有自我保护上限"


# --------------------------------------------------------------------------
# ⭐ 耗时的自洽性
#
# agent 正是靠这些数字推理的，所以它们不能是「各写各的随机数」。
# 之前真的出过这个 bug：下游自称 30ms，上游却只观察到 5ms。
# 单测当时只断言了结构（有哪些行），没断言数字关系，所以漏过去了。
# --------------------------------------------------------------------------


def _ms(text: str) -> int:
    match = re.search(r"(\d+)ms", text)
    assert match, f"这条日志里没有耗时：{text}"
    return int(match.group(1))


def test_logged_duration_matches_actual_time_gap() -> None:
    """日志里写的耗时，必须等于两个时间戳之间的真实间隔。"""
    system, store = _system()
    system.emit_request(T0)
    entries = store.all()

    received = next(e for e in entries if e.message == "received POST /api/v1/charge")
    done = next(e for e in entries if e.message.startswith("POST /api/v1/charge 200"))

    actual = int((done.ts - received.ts).total_seconds() * 1000)
    assert _ms(done.message) == actual, "自称的耗时和真实时间间隔对不上"


def test_upstream_observation_covers_downstream_self_report() -> None:
    """上游观察到的耗时必须 ≥ 下游自报的耗时（差值是网络往返）。

    反过来的话物理上不可能 —— 上游看到的时间不可能比下游自己花的还少。
    """
    system, store = _system()
    system.emit_request(T0)
    entries = store.all()

    for downstream, path in (
        ("payment-service", "/api/v1/charge"),
        ("inventory-service", "/api/v1/reserve"),
    ):
        self_reported = _ms(
            next(
                e.message
                for e in entries
                if e.service == downstream and e.message.startswith(f"POST {path} 200")
            )
        )
        observed = _ms(
            next(
                e.message
                for e in entries
                if e.service == "order-service"
                and e.message.startswith(f"{downstream} responded 200 in")
            )
        )
        assert observed >= self_reported, (
            f"{downstream}：上游观察到 {observed}ms，下游自报 {self_reported}ms —— 物理上不可能"
        )


def test_entry_total_covers_sum_of_downstream_observations() -> None:
    """入口服务的总耗时 ≥ 它对各下游观察到的耗时之和。"""
    system, store = _system()
    system.emit_request(T0)
    entries = store.all()

    observed = [
        _ms(e.message)
        for e in entries
        if e.service == "order-service" and "responded 200 in" in e.message
    ]
    total = _ms(next(e.message for e in entries if e.message.startswith("POST /api/v1/orders 200")))

    assert len(observed) == 2
    assert total >= sum(observed), f"总耗时 {total}ms 小于下游之和 {sum(observed)}ms"


# --------------------------------------------------------------------------
# 向后兼容：M1 的用法不能坏
# --------------------------------------------------------------------------


def test_m1_style_injection_still_works() -> None:
    """M1 的故障注入接受一个 MockService；从系统里取出来的应该能直接用。"""
    from fivewhys.mock.scenarios import inject_db_pool_exhausted

    system, store = _system()
    system.normal_operation(T0, T0 + timedelta(seconds=5), rps=2.0)
    before = len(store)

    truth = inject_db_pool_exhausted(
        store, system.service("order-service"), T0 + timedelta(seconds=5)
    )

    assert truth.root_cause_service == "order-service"
    assert len(store) > before
