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
                evidence=[Evidence(source="query_logs(order-service)", finding="大量 deadline exceeded")],
            )
        ],
        evidence=[Evidence(source="query_logs(order-service)", finding="connection wait time 飙升")],
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
# M1 的 TODO —— 实现完成后删掉 skip 标记
# --------------------------------------------------------------------------


def test_inject_db_pool_exhausted_not_implemented_yet() -> None:
    """M1-2 完成后，把这个测试改成真正的断言。"""
    store = LogStore()
    service = MockService("order-service", store)
    start = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)

    try:
        inject_db_pool_exhausted(store, service, start)
    except NotImplementedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("M1-2 尚未实现，这个测试应该先失败")
