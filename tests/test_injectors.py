"""FIV-10 验收测试：统一的注入器接口与注册表。

对应需求：FR-2（故障注入）

除了注册表本身，重点验证两条：
1. **新旧两个入口走同一份实现** —— 不能让它们各自演化出不同行为
2. **FaultScript 保证日志与指标同源** —— 这是最容易写错的地方
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fivewhys.mock import (
    INJECTORS,
    ConfigStore,
    DeployStore,
    InjectionContext,
    LogStore,
    MockSystem,
    available,
    catalogue,
    inject,
)
from fivewhys.mock.injectors import get, register
from fivewhys.mock.injectors._base import FaultScript
from fivewhys.mock.injectors.db_pool import inject_db_pool
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.mock.service import MockService
from fivewhys.models import FaultCategory, LogLevel

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


def test_db_pool_is_registered() -> None:
    assert FaultCategory.DB_POOL_EXHAUSTED in INJECTORS


def test_available_lists_registered_categories() -> None:
    assert FaultCategory.DB_POOL_EXHAUSTED in available()


def test_catalogue_is_readable() -> None:
    entries = catalogue()
    assert entries
    entry = next(e for e in entries if e["category"] == "db_pool_exhausted")
    assert entry["name"] == "db-pool-exhausted"
    assert entry["description"]


def test_get_returns_the_spec() -> None:
    spec = get(FaultCategory.DB_POOL_EXHAUSTED)
    assert spec.category is FaultCategory.DB_POOL_EXHAUSTED
    assert callable(spec.inject)


def test_unknown_category_raises_with_the_list() -> None:
    """报错信息要能告诉调用方「有哪些可用」。"""
    with pytest.raises(KeyError, match="没有注册故障"):
        get(FaultCategory.CERT_EXPIRED)


def test_duplicate_registration_is_rejected() -> None:
    """重复注册要在导入期就炸，而不是运行时悄悄覆盖。"""
    with pytest.raises(ValueError, match="重复注册"):

        @register(FaultCategory.DB_POOL_EXHAUSTED, "dup", "dup")
        def _dup(ctx: InjectionContext) -> object:  # pragma: no cover
            raise AssertionError("不该被调用")


# --------------------------------------------------------------------------
# InjectionContext
# --------------------------------------------------------------------------


def _system() -> MockSystem:
    return MockSystem(LogStore(), seed=0)


def test_context_exposes_the_stores() -> None:
    system = _system()
    ctx = InjectionContext(system=system, at=T0, target="order-service")

    assert ctx.logs is system.store
    assert ctx.metrics is system.metrics
    assert ctx.configs is system.configs
    assert ctx.deploys is system.deploys


def test_context_resolves_the_target_service() -> None:
    ctx = InjectionContext(system=_system(), at=T0, target="payment-service")
    assert ctx.service.name == "payment-service"


def test_context_rng_is_the_service_rng() -> None:
    """注入器必须用服务的随机源，场景才可复现。"""
    ctx = InjectionContext(system=_system(), at=T0, target="order-service")
    assert ctx.rng is ctx.service.rng


def test_context_rejects_unknown_target() -> None:
    ctx = InjectionContext(system=_system(), at=T0, target="nope")
    with pytest.raises(KeyError, match="未知服务"):
        _ = ctx.service


# --------------------------------------------------------------------------
# ⭐ 两个入口走同一份实现
# --------------------------------------------------------------------------


def test_registry_entry_and_direct_entry_produce_identical_results() -> None:
    """注册表入口和单服务便捷入口必须产出**完全一样**的东西。

    它们是同一个实现的两个门 —— 如果哪天有人只改了一边，
    这个测试会立刻炸。
    """

    def via_registry() -> tuple[list[str], list[str], list[str]]:
        system = MockSystem(LogStore(), seed=7)
        ctx = InjectionContext(system=system, at=T0, target="order-service")
        inject(FaultCategory.DB_POOL_EXHAUSTED, ctx)
        return (
            [entry.model_dump_json() for entry in system.store.all()],
            [str(sample) for sample in system.metrics.all()],
            [str(snapshot) for snapshot in system.configs.all()],
        )

    def via_shim() -> tuple[list[str], list[str], list[str]]:
        logs = LogStore()
        system = MockSystem(logs, seed=7)
        inject_db_pool_exhausted(
            logs,
            system.service("order-service"),
            T0,
            metrics=system.metrics,
            configs=system.configs,
        )
        return (
            [entry.model_dump_json() for entry in logs.all()],
            [str(sample) for sample in system.metrics.all()],
            [str(snapshot) for snapshot in system.configs.all()],
        )

    assert via_registry() == via_shim()


def test_registry_entry_returns_a_ground_truth() -> None:
    system = MockSystem(LogStore(), seed=0)
    truth = inject(
        FaultCategory.DB_POOL_EXHAUSTED,
        InjectionContext(system=system, at=T0, target="order-service"),
    )

    assert truth.root_cause_service == "order-service"
    assert truth.fault_category is FaultCategory.DB_POOL_EXHAUSTED


# --------------------------------------------------------------------------
# ⭐ FaultScript：日志与指标必须同源
# --------------------------------------------------------------------------


def _script(metrics: MetricStore | None = None) -> tuple[FaultScript, MockService]:
    service = MockService("order-service", LogStore(), seed=0)
    return FaultScript(service=service, metrics=metrics), service


def test_error_records_both_a_log_and_a_failure_sample() -> None:
    """一条 ERROR **必须**对应一条失败采样 —— 两者一一对应。"""
    metrics = MetricStore()
    script, _ = _script(metrics)
    script.error(T0, "boom", "trace-1", latency_ms=1234)
    script.flush()

    assert len(metrics) == 1
    sample = metrics.all()[0]
    assert sample.failed
    assert sample.latency_ms == 1234
    assert sample.ts == T0


def test_ok_records_both_a_log_and_a_success_sample() -> None:
    metrics = MetricStore()
    script, _ = _script(metrics)
    script.ok(T0, "fine", 42)
    script.flush()

    assert len(metrics) == 1
    assert not metrics.all()[0].failed


def test_no_metrics_means_logs_only() -> None:
    """不传指标仓库时只写日志 —— 向后兼容。"""
    script, _ = _script(metrics=None)
    script.error(T0, "boom")
    script.flush()
    assert len(script.service.store) == 1


def test_flush_sorts_events_by_time() -> None:
    """事件是交错生成的，写出去之前必须排序。"""
    script, service = _script()
    script.info(T0 + timedelta(seconds=10), "third")
    script.info(T0, "first")
    script.info(T0 + timedelta(seconds=5), "second")
    script.flush()

    messages = [entry.message for entry in service.store.all()]
    assert messages == ["first", "second", "third"]


def test_flush_preserves_trace_ids() -> None:
    script, service = _script()
    script.info(T0, "with trace", "abc123")
    script.info(T0 + timedelta(seconds=1), "without")
    script.flush()

    entries = service.store.all()
    assert entries[0].trace_id == "abc123"
    assert entries[1].trace_id is None


def test_levels_are_mapped_correctly() -> None:
    script, service = _script()
    script.info(T0, "i")
    script.warn(T0 + timedelta(seconds=1), "w")
    script.error(T0 + timedelta(seconds=2), "e")
    script.flush()

    assert [entry.level for entry in service.store.all()] == [
        LogLevel.INFO,
        LogLevel.WARN,
        LogLevel.ERROR,
    ]


def test_background_traffic_is_all_info() -> None:
    """背景噪声必须是纯 INFO —— 否则「正常场景」测不了误报。"""
    import random

    script, service = _script(MetricStore())
    script.background_traffic(T0, T0 + timedelta(seconds=10), rng=random.Random(0), interval_s=1)
    script.flush()

    assert len(service.store) > 0
    assert all(entry.level is LogLevel.INFO for entry in service.store.all())
    assert all(not sample.failed for sample in script.metrics.all())  # type: ignore[union-attr]


def test_background_traffic_is_reproducible() -> None:
    import random

    def build() -> list[str]:
        script, service = _script()
        script.background_traffic(
            T0, T0 + timedelta(seconds=10), rng=random.Random(3), interval_s=1
        )
        script.flush()
        return [entry.model_dump_json() for entry in service.store.all()]

    assert build() == build()


def test_flush_is_idempotent_for_metrics() -> None:
    """重复 flush 不该把采样写两遍。

    （不禁止重复 flush，但要求它不产生重复数据 —— 否则指标会翻倍。）
    """
    metrics = MetricStore()
    script, _ = _script(metrics)
    script.error(T0, "boom")
    script.flush()
    first = len(metrics)

    script.flush()
    assert len(metrics) == first, "第二次 flush 不该再写一遍"


# --------------------------------------------------------------------------
# 向后兼容
# --------------------------------------------------------------------------


def test_legacy_shim_still_produces_the_same_shape() -> None:
    logs = LogStore()
    service = MockService("order-service", logs, seed=0)
    metrics = MetricStore()
    configs = ConfigStore()
    # 先记一份基线配置 —— 没有基线就无所谓「变化」
    configs.record_values(T0 - timedelta(minutes=1), "order-service", {"db.pool_size": 50})

    truth = inject_db_pool_exhausted(logs, service, T0, metrics=metrics, configs=configs)

    assert truth.fault_category is FaultCategory.DB_POOL_EXHAUSTED
    assert len(logs) > 0
    assert len(metrics) > 0

    changes = configs.changes("order-service")
    assert changes, "注入器必须记下配置变更 —— 那是根因所在"
    assert changes[0].old == 50
    assert changes[0].new == 5


def test_direct_helper_is_importable_from_the_injector_module() -> None:
    """``inject_db_pool`` 是共享实现，两个入口都指向它。"""
    service = MockService("order-service", LogStore(), seed=0)
    truth = inject_db_pool(service=service, at=T0, rng=service.rng)
    assert truth.root_cause_service == "order-service"


def test_external_stores_are_used() -> None:
    """注入器必须用传进来的仓库，而不是自己 new 一个。"""
    system = MockSystem(
        LogStore(),
        metrics=MetricStore(),
        configs=ConfigStore(),
        deploys=DeployStore(),
        seed=0,
    )
    before_configs = len(system.configs)
    inject(
        FaultCategory.DB_POOL_EXHAUSTED,
        InjectionContext(system=system, at=T0, target="order-service"),
    )

    assert len(system.configs) > before_configs
    assert len(system.metrics) > 0
