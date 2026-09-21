"""FIV-9 验收测试：场景包落盘与校验。

对应需求：FR-15（场景包）、FR-2（日志不得泄漏答案）

最重要的是 `validate()` —— 它把「设计约束」变成了**可自动执行的断言**：
日志不许泄漏答案、question 不许泄漏判分词。靠人记得的东西迟早会忘。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fivewhys.mock import LogStore, MetricStore, MockService, RequestSample
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.models import FaultCategory, GroundTruth, LogLevel
from fivewhys.scenario import (
    LOGS_NAME,
    MANIFEST_NAME,
    METRICS_NAME,
    Scenario,
    ScenarioManifest,
    build_db_pool_scenario,
)

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


@pytest.fixture
def scenario() -> Scenario:
    return build_db_pool_scenario(seed=0, base_time=T0)


# --------------------------------------------------------------------------
# 构造出来的场景本身
# --------------------------------------------------------------------------


def test_built_scenario_is_valid(scenario: Scenario) -> None:
    problems = scenario.validate()
    assert problems == [], f"构造出来的场景应该没问题，实际：{problems}"
    assert scenario.is_valid


def test_scenario_has_three_services(scenario: Scenario) -> None:
    assert set(scenario.topology) == {
        "order-service",
        "payment-service",
        "inventory-service",
    }


def test_scenario_has_both_logs_and_metrics(scenario: Scenario) -> None:
    assert len(scenario.logs) > 0
    assert len(scenario.metrics) > 0


def test_ground_truth_points_at_order_service(scenario: Scenario) -> None:
    assert scenario.ground_truth.root_cause_service == "order-service"
    assert scenario.ground_truth.fault_category is FaultCategory.DB_POOL_EXHAUSTED


# --------------------------------------------------------------------------
# ⭐ question 的规格（FR-15）
# --------------------------------------------------------------------------


def test_question_provides_service_and_rough_time(scenario: Scenario) -> None:
    """给服务名和粗略时间 —— 难点留在「怎么找原因」，不是「怎么找时间」。"""
    assert "order-service" in scenario.question
    assert "14:02" in scenario.question


def test_question_does_not_leak_any_scoring_keyword(scenario: Scenario) -> None:
    """FR-15 的判据：question 里不得出现判分词。

    出现了就等于把答案写在题面上 —— 比如「order-service 的连接池好像有问题」。
    """
    lower = scenario.question.lower()
    leaked = [kw for kw in scenario.ground_truth.match_keywords if kw.lower() in lower]
    assert leaked == [], f"question 泄漏了判分词：{leaked}"


def test_question_does_not_contain_the_answer(scenario: Scenario) -> None:
    lower = scenario.question.lower()
    leaked = [kw for kw in scenario.ground_truth.answer_keywords if kw.lower() in lower]
    assert leaked == []


def test_question_does_not_leak_specific_error_text(scenario: Scenario) -> None:
    """不应该把日志里的具体报错直接写进题面。"""
    assert "deadline exceeded" not in scenario.question
    assert "connection wait" not in scenario.question


# --------------------------------------------------------------------------
# 落盘 / 加载
# --------------------------------------------------------------------------


def test_save_writes_expected_files(scenario: Scenario, tmp_path: Path) -> None:
    target = scenario.save(tmp_path)

    assert target.name == scenario.scenario_id
    assert (target / MANIFEST_NAME).exists()
    assert (target / LOGS_NAME).exists()
    assert (target / METRICS_NAME).exists()


def test_round_trip_preserves_everything(scenario: Scenario, tmp_path: Path) -> None:
    target = scenario.save(tmp_path)
    restored = Scenario.load(target)

    assert restored.scenario_id == scenario.scenario_id
    assert restored.question == scenario.question
    assert restored.ground_truth == scenario.ground_truth
    assert restored.topology == scenario.topology
    assert restored.logs.all() == scenario.logs.all()
    assert restored.metrics.all() == scenario.metrics.all()


def test_loaded_scenario_is_still_valid(scenario: Scenario, tmp_path: Path) -> None:
    restored = Scenario.load(scenario.save(tmp_path))
    assert restored.validate() == []


def test_load_reports_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=MANIFEST_NAME):
        Scenario.load(tmp_path)


def test_save_is_idempotent(scenario: Scenario, tmp_path: Path) -> None:
    """重复保存不应该出问题（文件被覆盖，内容一致）。"""
    first = scenario.save(tmp_path)
    before = (first / MANIFEST_NAME).read_text(encoding="utf-8")
    scenario.save(tmp_path)
    after = (first / MANIFEST_NAME).read_text(encoding="utf-8")

    assert before == after


# --------------------------------------------------------------------------
# ⭐ 校验能抓到问题
#
# 校验函数如果永远是「通过」，那它就等于不存在。
# 下面每个测试都人为制造一个问题，确认它被抓到。
# --------------------------------------------------------------------------


def _mutate(scenario: Scenario, **overrides: object) -> Scenario:
    manifest = scenario.manifest.model_copy(update=overrides)
    return Scenario(manifest=manifest, logs=scenario.logs, metrics=scenario.metrics)


def test_validate_catches_answer_leak_in_logs(scenario: Scenario) -> None:
    """日志里出现答案词必须被抓到 —— 这是 FR-2 的可执行判据。"""
    logs = LogStore()
    service = MockService("order-service", logs, seed=0)
    service.emit(LogLevel.ERROR, "database pool exhausted", T0)

    broken = Scenario(manifest=scenario.manifest, logs=logs, metrics=scenario.metrics)
    problems = broken.validate()
    assert any("泄漏了答案词" in p for p in problems), problems
    assert not broken.is_valid


def test_validate_catches_scoring_keyword_in_question(scenario: Scenario) -> None:
    broken = _mutate(scenario, question="order-service 的连接池好像耗尽了，看看")
    problems = broken.validate()
    assert any("question 里泄漏了判分词" in p for p in problems), problems


def test_validate_catches_empty_logs(scenario: Scenario) -> None:
    broken = Scenario(manifest=scenario.manifest, logs=LogStore(), metrics=scenario.metrics)
    assert any("日志为空" in p for p in broken.validate())


def test_validate_catches_empty_metrics(scenario: Scenario) -> None:
    broken = Scenario(manifest=scenario.manifest, logs=scenario.logs, metrics=MetricStore())
    assert any("指标为空" in p for p in broken.validate())


def test_validate_catches_missing_root_cause_service_in_topology(scenario: Scenario) -> None:
    broken = _mutate(scenario, topology={"payment-service": []})
    assert any("拓扑里没有根因服务" in p for p in broken.validate())


def test_validate_catches_failures_in_a_no_fault_scenario(scenario: Scenario) -> None:
    """正常场景（NO_FAULT）不该有任何失败采样 —— 否则它就不是「正常」的。"""
    truth = scenario.ground_truth.model_copy(update={"fault_category": FaultCategory.NO_FAULT})
    mutable_metrics = MetricStore()
    mutable_metrics.extend(scenario.metrics.all())
    mutable_metrics.record_request(T0, "order-service", 5000, status=500)

    broken = Scenario(
        manifest=scenario.manifest.model_copy(update={"ground_truth": truth}),
        logs=scenario.logs,
        metrics=mutable_metrics,
    )
    assert any("正常场景不该有失败请求" in p for p in broken.validate())


# --------------------------------------------------------------------------
# 可复现（NFR-1 / FR-4）
# --------------------------------------------------------------------------


def test_same_seed_builds_identical_scenario() -> None:
    """同一个种子必须产出逐字节一致的场景 —— 否则 M7 的改进曲线不可信。"""
    first = build_db_pool_scenario(seed=3, base_time=T0)
    second = build_db_pool_scenario(seed=3, base_time=T0)

    assert first.logs.all() == second.logs.all()
    assert first.metrics.all() == second.metrics.all()
    assert first.scenario_id == second.scenario_id


def test_different_seeds_produce_different_noise() -> None:
    """不同种子应该产出不同的噪声 —— 否则「5 次」测的是同一个场景。"""
    first = build_db_pool_scenario(seed=1, base_time=T0)
    second = build_db_pool_scenario(seed=2, base_time=T0)

    assert first.logs.all() != second.logs.all()
    # 但根因和故障类别必须一致
    assert first.ground_truth.fault_category == second.ground_truth.fault_category
    assert first.ground_truth.root_cause_service == second.ground_truth.root_cause_service


# --------------------------------------------------------------------------
# 其他
# --------------------------------------------------------------------------


def test_manifest_carries_a_version(scenario: Scenario, tmp_path: Path) -> None:
    """格式版本要落盘 —— 将来改结构时能靠它做兼容判断。"""
    target = scenario.save(tmp_path)
    import json

    raw = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert raw["version"] >= 1


def test_summary_is_readable(scenario: Scenario) -> None:
    text = scenario.summary()
    assert scenario.scenario_id in text
    assert "日志" in text and "指标" in text


def test_manifest_is_serialisable() -> None:
    manifest = ScenarioManifest(
        scenario_id="s1",
        question="q",
        ground_truth=GroundTruth(
            scenario_id="s1",
            fault_category=FaultCategory.NO_FAULT,
            root_cause_service="order-service",
            root_cause="无故障",
            injected_at=T0,
            symptoms=[],
            match_keywords=[],
        ),
    )
    assert manifest.version >= 1
    assert ScenarioManifest.model_validate_json(manifest.model_dump_json()) == manifest


def test_build_uses_warmup_window(scenario: Scenario) -> None:
    """故障之前必须有正常流量 —— 否则「什么时候开始坏的」无从判断。"""
    fault_at = scenario.ground_truth.injected_at
    before = [e for e in scenario.logs.all() if e.ts < fault_at]
    assert before, "故障前应该有正常流量"

    healthy = scenario.metrics.query("order-service", T0, fault_at - timedelta(seconds=1))
    assert healthy and all(b.error_rate == 0.0 for b in healthy)


def test_injected_fault_reflected_in_metrics(scenario: Scenario) -> None:
    """场景包里指标必须已经反映了故障 —— 否则 agent 会以为「监控没报警」。"""
    fault_at = scenario.ground_truth.injected_at
    dirty = scenario.metrics.query("order-service", fault_at, fault_at + timedelta(minutes=1))
    assert dirty
    assert dirty[0].error_rate > 0


def test_request_sample_importable_from_scenario_module() -> None:
    """FIV-9 之后，评测台会从这里拿数据。"""
    assert RequestSample is not None
    assert inject_db_pool_exhausted is not None
