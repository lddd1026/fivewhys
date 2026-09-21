"""FIV-11 验收测试：五种故障 + 正常场景。

对应需求：FR-2（故障注入）、§10.2（正常场景与误报率）

除了"能不能构造出来"，重点验证每种故障**各自考什么**：

- ``dependency_5xx``   根因在下游，不在被问的服务上
- ``memory_leak``      渐进式，没有突然的跳变点
- ``cert_expired``     只打中一条调用链
- ``bad_config_rollout`` 要靠发布历史才能关联
- ``no_fault``         一条失败采样都不能有
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fivewhys.mock import available, catalogue
from fivewhys.models import FaultCategory
from fivewhys.scenario import Scenario, build_all_scenarios, build_scenario

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

ALL_CATEGORIES = [
    FaultCategory.DB_POOL_EXHAUSTED,
    FaultCategory.DEPENDENCY_5XX,
    FaultCategory.MEMORY_LEAK,
    FaultCategory.CERT_EXPIRED,
    FaultCategory.BAD_CONFIG_ROLLOUT,
    FaultCategory.NO_FAULT,
]


def _scenario(category: FaultCategory, seed: int = 0) -> Scenario:
    return build_scenario(category, seed=seed, base_time=T0, warmup_minutes=2)


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


def test_all_six_categories_are_registered() -> None:
    assert set(available()) == set(ALL_CATEGORIES)
    assert len(available()) == 6


def test_catalogue_describes_every_fault() -> None:
    entries = catalogue()
    assert len(entries) == 6
    for entry in entries:
        assert entry["name"], "每种故障都要有个短名字"
        assert entry["description"], "每种故障都要有一句人话说明"


# --------------------------------------------------------------------------
# ⭐ 共用不变量：答案词只能出现在配置里
# --------------------------------------------------------------------------


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_answer_keywords_never_appear_in_logs(category: FaultCategory) -> None:
    """所有故障都适用的铁律：日志是现象，答案在配置里。

    这条检查如果放在每个注入器里各写一遍，迟早有人漏掉。
    放在这里统一跑 —— 新加故障时会自动被覆盖。
    """
    scenario = _scenario(category)
    log_text = " ".join(entry.message.lower() for entry in scenario.logs.all())

    for keyword in scenario.ground_truth.answer_keywords:
        assert keyword.lower() not in log_text, (
            f"{category.value} 的日志里泄漏了答案词「{keyword}」"
        )


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_every_scenario_builds_and_validates(category: FaultCategory) -> None:
    scenario = _scenario(category)
    assert scenario.validate() == [], scenario.validate()


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_every_scenario_has_all_five_data_kinds(category: FaultCategory) -> None:
    scenario = _scenario(category)
    assert len(scenario.logs) > 0
    assert len(scenario.metrics) > 0
    assert len(scenario.configs) > 0
    assert len(scenario.deploys) > 0
    assert scenario.topology


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_every_scenario_is_reproducible(category: FaultCategory) -> None:
    """同一个种子必须产出完全一致的场景（NFR-1）。"""

    def build() -> list[str]:
        scenario = _scenario(category, seed=13)
        return [
            *(entry.model_dump_json() for entry in scenario.logs.all()),
            *(str(sample) for sample in scenario.metrics.all()),
            *(str(snapshot) for snapshot in scenario.configs.all()),
        ]

    assert build() == build()


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_question_never_leaks_scoring_keywords(category: FaultCategory) -> None:
    """FR-15：题面里不得出现判分词。"""
    scenario = _scenario(category)
    lower = scenario.question.lower()
    leaked = [kw for kw in scenario.ground_truth.match_keywords if kw.lower() in lower]
    assert leaked == [], f"题面泄漏了判分词：{leaked}"


# --------------------------------------------------------------------------
# ⭐ dependency_5xx：根因不在被问的服务上
# --------------------------------------------------------------------------


def test_dependency_5xx_root_cause_is_the_downstream() -> None:
    """这是整个场景的意义：症状在 order-service，根因在 inventory-service。

    agent 只盯着题面提到的服务，是找不到根因的。
    """
    scenario = _scenario(FaultCategory.DEPENDENCY_5XX)

    assert scenario.ground_truth.root_cause_service == "inventory-service"
    assert "order-service" in scenario.question, "题面问的是入口服务"
    assert scenario.ground_truth.root_cause_service not in scenario.question


def test_dependency_5xx_symptoms_appear_on_both_services() -> None:
    """上游报 503，下游报连不上数据库 —— 两边都要有痕迹，否则跨不过服务边界。"""
    scenario = _scenario(FaultCategory.DEPENDENCY_5XX)

    order_errors = [
        e.message
        for e in scenario.logs.all()
        if e.service == "order-service" and e.level.value == "ERROR"
    ]
    culprit_errors = [
        e.message
        for e in scenario.logs.all()
        if e.service == "inventory-service" and e.level.value == "ERROR"
    ]

    assert any("503" in m for m in order_errors), order_errors[:3]
    assert any("stock-db" in m for m in culprit_errors), culprit_errors[:3]


def test_dependency_5xx_downstream_is_otherwise_healthy() -> None:
    """被打中的下游自己也在正常处理请求 —— 只是上游连不上它。"""
    scenario = _scenario(FaultCategory.DEPENDENCY_5XX)
    info_count = sum(
        1
        for e in scenario.logs.all()
        if e.service == "inventory-service" and e.level.value == "INFO"
    )
    assert info_count > 0


# --------------------------------------------------------------------------
# ⭐ memory_leak：渐进式，没有突然的跳变点
# --------------------------------------------------------------------------


def test_memory_leak_latency_climbs_gradually() -> None:
    """延迟应该是**爬升**的，不是某一刻突然跳上去。

    如果 agent 只会找「什么时候突然变了」，这个场景会让它一无所获。
    """
    scenario = _scenario(FaultCategory.MEMORY_LEAK)
    start = scenario.ground_truth.injected_at

    buckets = scenario.metrics.query("order-service", start, start + timedelta(minutes=8))
    assert len(buckets) >= 4, "窗口内应该有多个指标桶"

    # 至少要有一次 OOM 重启带来的错误
    assert any(b.errors > 0 for b in buckets)

    # 非重启的桶里，延迟应该整体上升而不是原地不动
    normal_buckets = [b for b in buckets if b.errors == 0]
    assert len(normal_buckets) >= 3
    first_p95 = normal_buckets[0].p95_latency_ms
    last_p95 = normal_buckets[-1].p95_latency_ms
    assert last_p95 > first_p95, (
        f"延迟应该随堆占用上升：首个非重启桶 P95={first_p95}ms，最后={last_p95}ms"
    )


def test_memory_leak_logs_heap_growth() -> None:
    scenario = _scenario(FaultCategory.MEMORY_LEAK)
    heap_lines = [e.message for e in scenario.logs.all() if "heap usage" in e.message]
    assert len(heap_lines) >= 3, "应该有多次堆占用告警，体现增长过程"


def test_memory_leak_has_oom_restarts() -> None:
    scenario = _scenario(FaultCategory.MEMORY_LEAK)
    oom = [e.message for e in scenario.logs.all() if "OOMKilled" in e.message]
    assert len(oom) >= 1, "应该有 OOM 重启"


# --------------------------------------------------------------------------
# ⭐ cert_expired：只打中一条调用链
# --------------------------------------------------------------------------


def test_cert_expired_failures_are_confined_to_one_dependency() -> None:
    """错误信息里反复出现 payment-service，但**只有**那条链路失败。"""
    scenario = _scenario(FaultCategory.CERT_EXPIRED)
    errors = [e.message for e in scenario.logs.all() if e.level.value == "ERROR"]

    assert errors
    assert all("payment-service" in m for m in errors), (
        f"失败应该全部集中在 payment-service 这条链路：{set(errors)}"
    )


def test_cert_expired_other_dependency_is_healthy() -> None:
    scenario = _scenario(FaultCategory.CERT_EXPIRED)
    inventory_info = [
        e
        for e in scenario.logs.all()
        if e.service == "inventory-service" and e.level.value == "INFO"
    ]
    assert inventory_info, "对照组服务应该完全正常"


def test_cert_expired_root_cause_is_the_caller_not_the_callee() -> None:
    """证书是**发起调用那一方**的，不是被调用的 payment-service。

    错误信息里老是出现 payment-service，很容易被误导成根因服务。
    """
    scenario = _scenario(FaultCategory.CERT_EXPIRED)
    assert scenario.ground_truth.root_cause_service == "order-service"


def test_cert_expired_log_does_not_name_the_reason() -> None:
    """日志只说 TLS 握手失败，不说是为什么 —— 查配置才知道是证书到期。"""
    scenario = _scenario(FaultCategory.CERT_EXPIRED)
    text = " ".join(e.message.lower() for e in scenario.logs.all())
    assert "expired" not in text
    assert "x509" not in text


# --------------------------------------------------------------------------
# ⭐ bad_config_rollout：要靠发布历史才能关联
# --------------------------------------------------------------------------


def test_bad_rollout_has_a_deploy_record() -> None:
    scenario = _scenario(FaultCategory.BAD_CONFIG_ROLLOUT)
    deploys = scenario.deploys.history("order-service")
    assert len(deploys) >= 2, "应该有基线发布 + 那次坏发布"

    bad = deploys[-1]
    assert bad.version == "v1.4.2"
    assert bad.operator == "alice"


def test_bad_rollout_deploy_is_close_to_the_errors() -> None:
    """发布时刻必须紧挨着错误开始 —— 否则关联不起来。"""
    scenario = _scenario(FaultCategory.BAD_CONFIG_ROLLOUT)
    deploy_ts = scenario.deploys.history("order-service")[-1].ts

    assert abs((deploy_ts - scenario.ground_truth.injected_at).total_seconds()) <= 5


def test_bad_rollout_config_change_is_attributed_to_the_deploy() -> None:
    """配置变更的原因要写清楚是发布引起的 —— 这决定了处置方式（回滚 vs 改回配置）。"""
    scenario = _scenario(FaultCategory.BAD_CONFIG_ROLLOUT)
    changes = scenario.configs.changes("order-service")
    assert any("retry.max" in c.key for c in changes)

    snapshots = scenario.configs.history("order-service")
    assert any("deploy" in s.note.lower() for s in snapshots)


def test_bad_rollout_is_distinguishable_from_a_manual_config_edit() -> None:
    """这是它和 db_pool 的关键区别：一个有发布记录，一个没有。"""
    rollout = _scenario(FaultCategory.BAD_CONFIG_ROLLOUT)
    db_pool = _scenario(FaultCategory.DB_POOL_EXHAUSTED)

    assert len(rollout.deploys) > 3, "坏发布场景应该多一条发布记录"
    assert len(db_pool.deploys) == 3, "db_pool 只有基线发布"


# --------------------------------------------------------------------------
# ⭐ no_fault：一条失败采样都不能有
# --------------------------------------------------------------------------


def test_no_fault_has_zero_failures() -> None:
    scenario = _scenario(FaultCategory.NO_FAULT)
    failed = [s for s in scenario.metrics.all() if s.failed]
    assert failed == [], f"健康场景不该有失败请求，实际有 {len(failed)} 条"


def test_no_fault_has_no_error_logs() -> None:
    scenario = _scenario(FaultCategory.NO_FAULT)
    errors = [e.message for e in scenario.logs.all() if e.level.value in {"ERROR", "WARN"}]
    assert errors == [], f"健康场景不该有 WARN/ERROR，实际有 {len(errors)} 条"


def test_no_fault_error_rate_is_zero_everywhere() -> None:
    scenario = _scenario(FaultCategory.NO_FAULT)
    for service in scenario.topology:
        for bucket in scenario.metrics.query(
            service, T0, scenario.ground_truth.injected_at.replace(year=2027)
        ):
            assert bucket.error_rate == 0.0, f"{service} 在 {bucket.start} 有错误"


def test_no_fault_question_asks_for_confirmation() -> None:
    """健康场景的题面应该请 agent 确认，而不是断言"有问题"。"""
    scenario = _scenario(FaultCategory.NO_FAULT)
    assert "确认" in scenario.question


def test_no_fault_has_no_answer_keywords() -> None:
    """健康场景没有"答案"可泄漏 —— 日志里本来就不该有故障信息。"""
    scenario = _scenario(FaultCategory.NO_FAULT)
    assert scenario.ground_truth.answer_keywords == []


# --------------------------------------------------------------------------
# 全量构造（M6 会用到）
# --------------------------------------------------------------------------


def test_build_all_scenarios_covers_every_registered_fault() -> None:
    scenarios = build_all_scenarios(seed=0, base_time=T0)
    assert len(scenarios) == len(available())

    categories = {s.ground_truth.fault_category for s in scenarios}
    assert categories == set(ALL_CATEGORIES)


def test_all_scenarios_have_distinct_ids() -> None:
    scenarios = build_all_scenarios(seed=0, base_time=T0)
    ids = [s.scenario_id for s in scenarios]
    assert len(ids) == len(set(ids)), f"场景 ID 有重复：{ids}"


def test_build_all_scenarios_is_reproducible() -> None:
    first = [s.scenario_id for s in build_all_scenarios(seed=4, base_time=T0)]
    second = [s.scenario_id for s in build_all_scenarios(seed=4, base_time=T0)]
    assert first == second
