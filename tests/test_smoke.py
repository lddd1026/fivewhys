"""M0 冒烟测试。

目标不是覆盖业务逻辑（业务还没写），而是保证**骨架是活的**：
包能导入、模型能序列化、CLI 能跑、mock 系统能产日志。

每次改完代码都跑一遍：
    pytest
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from fivewhys import __version__
from fivewhys.cli import app
from fivewhys.config import Settings
from fivewhys.mock import LogStore, MockService
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.models import (
    AgentRun,
    Confidence,
    Diagnosis,
    Evidence,
    FaultCategory,
    GroundTruth,
    LogLevel,
    WhyStep,
)
from fivewhys.tools import Tool, ToolRegistry

runner = CliRunner()


# --------------------------------------------------------------------------
# 包与 CLI
# --------------------------------------------------------------------------


def test_version_string() -> None:
    assert __version__ == "0.1.0"


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_cli_doctor_passes() -> None:
    """M0 的验收标准：doctor 全绿。"""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "环境就绪" in result.stdout


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_model == "deepseek/deepseek-chat"
    assert settings.max_why_depth == 5
    assert settings.temperature == 0.0


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------


def test_diagnosis_roundtrip() -> None:
    diagnosis = Diagnosis(
        root_cause="连接池上限被下调",
        root_cause_service="order-service",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        confidence=Confidence.HIGH,
        why_chain=[
            WhyStep(
                depth=1,
                question="为什么错误率飙升？",
                answer="请求超时",
                evidence=[
                    Evidence(source="query_logs(order-service)", finding="大量 deadline exceeded")
                ],
            )
        ],
        evidence=[
            Evidence(source="query_logs(order-service)", finding="connection wait time 飙升")
        ],
        ruled_out=["下游 inventory-service 无异常日志"],
        suggested_fix="回滚配置",
        summary="连接池耗尽",
    )

    restored = Diagnosis.model_validate_json(diagnosis.model_dump_json())
    assert restored == diagnosis
    assert restored.fault_category is FaultCategory.DB_POOL_EXHAUSTED


def test_ground_truth_requires_keywords() -> None:
    gt = GroundTruth(
        scenario_id="s1",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        root_cause_service="order-service",
        root_cause="连接池耗尽",
        injected_at=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        symptoms=["错误率飙升"],
        match_keywords=["connection", "pool"],
    )
    assert gt.scenario_id == "s1"


def test_agent_run_duration() -> None:
    run = AgentRun(scenario_id="s1", model="deepseek/deepseek-chat")
    assert run.duration_s is None  # 还没结束
    run.finished_at = run.started_at + timedelta(seconds=12)
    assert run.duration_s == 12.0


# --------------------------------------------------------------------------
# 工具注册表
# --------------------------------------------------------------------------


def _dummy_tool() -> Tool:
    return Tool(
        name="query_logs",
        description="查日志",
        args_model=Evidence,  # 借用一下，只要能当参数模型就行
        func=lambda **kwargs: "ok",
    )


def test_tool_registry_register_and_get() -> None:
    registry = ToolRegistry()
    tool = registry.register(_dummy_tool())

    assert len(registry) == 1
    assert "query_logs" in registry
    assert registry.get("query_logs") is tool
    assert registry.names() == ["query_logs"]


def test_tool_registry_rejects_duplicates() -> None:
    registry = ToolRegistry()
    registry.register(_dummy_tool())
    try:
        registry.register(_dummy_tool())
    except ValueError as exc:
        assert "重名" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复注册应该报错")


def test_tool_spec_shape() -> None:
    spec = _dummy_tool().to_openai_spec()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "query_logs"
    assert "properties" in spec["function"]["parameters"]


# --------------------------------------------------------------------------
# mock 系统
# --------------------------------------------------------------------------


def test_mock_service_emits_logs() -> None:
    store = LogStore()
    service = MockService("order-service", store)
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    count = service.normal_operation(start, start + timedelta(seconds=10), rps=5.0)

    assert count > 0
    assert len(store) == count
    assert all(entry.service == "order-service" for entry in store.all())
    assert store.stats()[str(LogLevel.INFO)] == count


def test_logstore_jsonl_roundtrip(tmp_path) -> None:
    store = LogStore()
    service = MockService("order-service", store)
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    service.normal_operation(start, start + timedelta(seconds=3))

    path = store.dump_jsonl(tmp_path / "logs.jsonl")
    restored = LogStore.load_jsonl(path)

    assert len(restored) == len(store)
    assert restored.all() == store.all()


def test_mock_service_is_reproducible() -> None:
    """固定种子 —— 场景必须可复现，否则评测无从谈起。"""
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    def build() -> list[str]:
        store = LogStore()
        MockService("svc", store).normal_operation(start, start + timedelta(seconds=5))
        return [e.message for e in store.all()]

    assert build() == build()


# --------------------------------------------------------------------------
# FIV-1 · 故障注入
# --------------------------------------------------------------------------


def test_inject_db_pool_exhausted() -> None:
    """FIV-1 验收：故障注入正确，且日志不泄漏答案。"""
    store = LogStore()
    service = MockService("order-service", store)
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    n_normal = service.normal_operation(start, start + timedelta(minutes=30))
    normal_msgs = [e.message for e in store.all()]
    assert n_normal > 0

    fault_at = start + timedelta(minutes=30)
    gt = inject_db_pool_exhausted(store, service, fault_at)

    # 按数量切片拿到注入产生的那部分。
    # 不能按时间过滤（e.ts >= fault_at）—— 配置重载那条在 fault_at 前 2 秒，
    # 会被漏掉，而「不泄漏答案」的断言必须覆盖全部注入日志。
    injected = store.all()[n_normal:]
    assert len(injected) > 0

    # --- ground truth 正确 ---
    assert gt.fault_category is FaultCategory.DB_POOL_EXHAUSTED
    assert gt.root_cause_service == "order-service"
    assert gt.injected_at == fault_at
    assert gt.match_keywords

    # --- 错误数量落在合理区间 ---
    errors = [e for e in injected if e.level == LogLevel.ERROR]
    assert 10 <= len(errors) <= 30

    # --- 时间戳不减（排序写入保证了这一点）---
    timestamps = [e.ts for e in injected]
    assert timestamps == sorted(timestamps)

    # --- 故障期间仍有正常流量，否则 agent 一眼就看出来 ---
    info_during_fault = [e for e in injected if e.level == LogLevel.INFO]
    assert len(info_during_fault) > 1

    # --- 没有污染注入前的日志 ---
    assert [e.message for e in store.all()[:n_normal]] == normal_msgs

    # --- ⭐ 核心约束：日志不得泄漏【答案词】---
    # 注意用 answer_keywords 而不是 match_keywords：
    # "connection" 是关键线索、必须出现在日志里，它在 match_keywords 里但不在
    # answer_keywords 里。用错会导致线索被误判成泄漏。
    text = " ".join(e.message.lower() for e in injected)
    for keyword in gt.answer_keywords:
        assert keyword.lower() not in text, f"日志泄漏了答案词「{keyword}」"

    # --- 反过来：关键线索必须出现，否则 agent 无从推理 ---
    assert "connection wait time" in text, "日志里缺少指向连接池的关键线索"
    assert gt.answer_keywords, "必须定义答案词，否则泄漏检查形同虚设"


def test_injection_is_reproducible() -> None:
    """同一个 seed 跑两次必须完全一致 —— 这是评测的前提。"""
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    def build() -> list[str]:
        store = LogStore()
        svc = MockService("order-service", store)
        inject_db_pool_exhausted(store, svc, start)
        return [e.model_dump_json() for e in store.all()]

    assert build() == build()
