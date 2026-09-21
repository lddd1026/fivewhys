"""FIV-3 验收测试：工具的注册与组装。

对应需求：FR-5（排障工具）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.mock.service import MockService
from fivewhys.tools import ToolRegistry, build_registry

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
FAULT_AT = T0 + timedelta(minutes=10)


def _store() -> LogStore:
    store = LogStore()
    svc = MockService("order-service", store)
    svc.normal_operation(T0, FAULT_AT)
    inject_db_pool_exhausted(store, svc, FAULT_AT)
    return store


def test_build_registry_contains_query_logs() -> None:
    registry = build_registry(_store())
    assert "query_logs" in registry
    assert registry.names() == ["query_logs"]
    assert len(registry) == 1


def test_each_registry_is_independent() -> None:
    """每个场景一个独立 registry —— 并发跑评测时不能互相污染。"""
    first, second = build_registry(_store()), build_registry(_store())
    assert first is not second
    assert first.get("query_logs") is not second.get("query_logs")


def test_specs_are_valid_openai_function_schemas() -> None:
    """给模型看的说明书必须格式正确，否则 function calling 会失败。"""
    specs = build_registry(_store()).specs()
    assert len(specs) == 1

    spec = specs[0]
    assert spec["type"] == "function"

    function = spec["function"]
    assert function["name"] == "query_logs"
    assert function["description"]

    params = function["parameters"]
    assert params["type"] == "object"
    assert "service" in params["properties"]
    assert "start" in params["properties"]
    assert "end" in params["properties"]


def test_tool_call_goes_through_the_registry() -> None:
    """模拟 agent 的调用路径：模型给出工具名 + 参数 → 注册表执行。"""
    result = build_registry(_store()).get("query_logs")(
        service="order-service",
        start=FAULT_AT,
        end=FAULT_AT + timedelta(minutes=5),
    )
    assert "共命中" in result
    assert "ERROR" in result


def test_unknown_tool_raises_with_helpful_message() -> None:
    """模型可能编造工具名，报错信息要能帮它纠正。"""
    with pytest.raises(KeyError, match="未知工具"):
        build_registry(_store()).get("query_metrics")


def test_missing_arguments_are_rejected_before_execution() -> None:
    """参数校验发生在业务逻辑之前 —— 这是用 Pydantic 声明的价值。"""
    with pytest.raises(ValidationError):
        build_registry(_store()).get("query_logs")(service="order-service")


def test_limit_upper_bound_is_enforced() -> None:
    """limit 有上限，防止模型一次性要求几万条把上下文撑爆。"""
    with pytest.raises(ValidationError):
        build_registry(_store()).get("query_logs")(
            service="order-service",
            start=FAULT_AT,
            end=FAULT_AT + timedelta(minutes=5),
            limit=9999,
        )


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    tool = build_registry(_store()).get("query_logs")
    registry.register(tool)
    with pytest.raises(ValueError, match="重名"):
        registry.register(tool)
