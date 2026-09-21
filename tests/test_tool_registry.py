"""FIV-3 / FIV-13 验收测试：工具的注册与组装。

对应需求：FR-5（排障工具）、NFR-11（上下文预算）

工具层是 agent 的**双手**，所以这里要盯住两件事：

1. 说明书（JSON Schema）必须合法 —— 不合法 function calling 直接失败
2. 工具**知道自己在哪个场景里** —— 尤其是模型写错服务名时，
   不能让「查不到」被当成「没问题」
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.mock.service import MockService
from fivewhys.tools import DataSource, ToolRegistry, build_registry

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
FAULT_AT = T0 + timedelta(minutes=10)

# FIV-13 之后的工具集：按数据源切分的 5 个工具（M7 的对照组 A）。
EXPECTED_TOOLS = [
    "query_metrics",
    "query_logs",
    "get_config",
    "get_deploy_history",
    "get_dependencies",
]


def _source() -> DataSource:
    store = LogStore()
    svc = MockService("order-service", store)
    svc.normal_operation(T0, FAULT_AT)
    inject_db_pool_exhausted(store, svc, FAULT_AT)
    return DataSource.logs_only(store)


def _registry() -> ToolRegistry:
    return build_registry(_source())


def test_build_registry_contains_all_five_tools() -> None:
    registry = _registry()
    assert registry.names() == sorted(EXPECTED_TOOLS)
    assert len(registry) == 5


def test_tools_are_ordered_like_a_real_investigation() -> None:
    """工具顺序就是提示词里给模型看的顺序，按一次真实排障的先后排。

    ``names()`` 是排序过的（便于断言），这里单独盯顺序 ——
    顺序变了就是提示词变了，那会影响准确率，属于该被测试发现的事。
    """
    assert [spec["function"]["name"] for spec in _registry().specs()] == EXPECTED_TOOLS


def test_each_registry_is_independent() -> None:
    """每个场景一个独立 registry —— 并发跑评测时不能互相污染。"""
    first, second = _registry(), _registry()
    assert first is not second
    assert first.get("query_logs") is not second.get("query_logs")


def test_specs_are_valid_openai_function_schemas() -> None:
    """给模型看的说明书必须格式正确，否则 function calling 会失败。"""
    specs = _registry().specs()
    assert len(specs) == len(EXPECTED_TOOLS)

    for spec in specs:
        assert spec["type"] == "function"

        function = spec["function"]
        assert function["name"] in EXPECTED_TOOLS
        assert function["description"], f"{function['name']} 缺少描述"

        params = function["parameters"]
        assert params["type"] == "object"
        assert params["properties"], f"{function['name']} 没有参数"


def test_every_tool_description_teaches_a_method() -> None:
    """工具描述不只是「这个工具干什么」，还要教**什么时候用它**。

    这是需求 FR-5 的隐含要求：agent 面对 5 个工具，得知道先查哪个。
    描述里没有「先 / 第一步 / 典型用法」这类指引的工具，等于把选择难题丢给模型。
    """
    hints = ("先", "第一步", "典型用法", "用法", "如果")
    for spec in _registry().specs():
        description = spec["function"]["description"]
        assert any(hint in description for hint in hints), (
            f"{spec['function']['name']} 的描述没教方法：{description}"
        )


def test_query_logs_schema_declares_the_core_arguments() -> None:
    spec = next(spec for spec in _registry().specs() if spec["function"]["name"] == "query_logs")
    properties = spec["function"]["parameters"]["properties"]
    assert {"service", "start", "end"} <= set(properties)


def test_tool_call_goes_through_the_registry() -> None:
    """模拟 agent 的调用路径：模型给出工具名 + 参数 → 注册表执行。"""
    result = _registry().get("query_logs")(
        service="order-service",
        start=FAULT_AT,
        end=FAULT_AT + timedelta(minutes=5),
    )
    assert "共命中" in result
    assert "ERROR" in result


def test_unknown_tool_raises_with_helpful_message() -> None:
    """模型可能编造工具名，报错信息要能帮它纠正。"""
    with pytest.raises(KeyError, match="未知工具"):
        _registry().get("query_traces")


def test_missing_arguments_are_rejected_before_execution() -> None:
    """参数校验发生在业务逻辑之前 —— 这是用 Pydantic 声明的价值。"""
    with pytest.raises(ValidationError):
        _registry().get("query_logs")(service="order-service")


def test_limit_upper_bound_is_enforced() -> None:
    """limit 有上限，防止模型一次性要求几万条把上下文撑爆。"""
    with pytest.raises(ValidationError):
        _registry().get("query_logs")(
            service="order-service",
            start=FAULT_AT,
            end=FAULT_AT + timedelta(minutes=5),
            limit=9999,
        )


def test_metrics_bucket_size_is_bounded() -> None:
    """聚合粒度也要有上下界 —— 否则模型可以要 1 秒粒度的 100 万个桶。"""
    tool = _registry().get("query_metrics")
    with pytest.raises(ValidationError):
        tool(service="order-service", start=FAULT_AT, end=FAULT_AT, bucket_seconds=0)
    with pytest.raises(ValidationError):
        tool(service="order-service", start=FAULT_AT, end=FAULT_AT, bucket_seconds=100000)


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    tool = _registry().get("query_logs")
    registry.register(tool)
    with pytest.raises(ValueError, match="重名"):
        registry.register(tool)


# --------------------------------------------------------------------------
# 场景感知：工具得知道自己在哪个场景里
# --------------------------------------------------------------------------


def test_scenario_services_reach_the_tools() -> None:
    """服务名写错时必须**明确指出**，而不是回一句「这段时间没数据」。

    这条是 FIV-13 手工验证时想清楚的坑：日志仓库里没有「服务清单」，
    光看它分不清「服务不存在」和「服务存在但这段时间没日志」。
    前者被当成后者，就等于**把一次笔误当成一条排除性证据**。
    """
    registry = build_registry(
        DataSource(
            logs=_source().logs,
            metrics=_source().metrics,
            configs=_source().configs,
            deploys=_source().deploys,
            topology={"order-service": ["payment-service"], "payment-service": []},
        )
    )

    result = registry.get("query_logs")(
        service="payments-service",  # 手滑多打了个 s
        start=FAULT_AT,
        end=FAULT_AT + timedelta(minutes=5),
    )

    assert "没有服务" in result
    assert "order-service" in result and "payment-service" in result


def test_tools_without_any_service_list_still_work() -> None:
    """完全没有服务清单时（空场景），工具退化成「照常查询」，不能报错。

    ``DataSource.logs_only`` 仍然能从日志里推断出服务名，
    所以这里的「空清单」要真的空 —— 用一个什么都没有的数据源。
    """
    registry = build_registry(DataSource.logs_only(LogStore()))
    assert registry.get("query_logs")(
        service="whatever-service",
        start=FAULT_AT,
        end=FAULT_AT + timedelta(minutes=5),
    ).startswith("共命中 0 条")


def test_data_source_reports_services_from_topology_first() -> None:
    """拓扑是场景的定义，比「某个数据源恰好有数据」更可信。"""
    source = _source()
    assert source.services == ["order-service"], "没有拓扑时退回各数据源出现过的服务"

    with_topology = DataSource(
        logs=source.logs,
        metrics=source.metrics,
        configs=source.configs,
        deploys=source.deploys,
        topology={"a": [], "b": ["a"], "c": ["b"]},
    )
    assert with_topology.services == ["a", "b", "c"]
