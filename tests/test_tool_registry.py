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
from fivewhys.tools import (
    TOOL_DESCRIPTION_BUDGET_CHARS,
    DataSource,
    ToolRegistry,
    build_registry,
)

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


# --------------------------------------------------------------------------
# FIV-14：工具说明书的质量
#
# 说明书就是提示词的一部分，而且是**每次请求都发**的那部分。
# 下面这些断言把「描述该写什么」变成机器可检查的规矩：
# 位置、局限、典型调用、没有废话、总量有预算。
# --------------------------------------------------------------------------


def test_exactly_one_tool_claims_to_be_the_first_step() -> None:
    """⭐ 只能有一个「第一步」。

    这条是 FIV-14 通读说明书时发现的真问题：``query_metrics`` 和 ``query_logs``
    都写着「排障的第一步通常就是它」—— 模型没法同时听两个人的。
    两个工具都说自己是第一步，等于都没说。
    """
    names = [
        spec["function"]["name"]
        for spec in _registry().specs()
        if "第一步" in spec["function"]["description"]
    ]
    assert names == ["query_metrics"], f"说自己是第一步的工具：{names}"


def test_every_description_states_its_place_in_the_path() -> None:
    """每个工具都要说清自己在排障路径上的位置 —— 模型才知道先查哪个。

    允许「没有固定位置」这类回答：``get_dependencies`` 确实不属于任何一步，
    但**必须明说**，而不是含糊过去。
    """
    markers = ("第一步", "第二步", "第三步", "第四步", "第五步", "两个用法", "没有固定位置")
    for spec in _registry().specs():
        description = spec["function"]["description"]
        assert any(marker in description for marker in markers), (
            f"{spec['function']['name']} 没说清自己在排障路径上的位置"
        )


def test_every_description_states_a_limitation() -> None:
    """每个工具都要说清**什么时候不该用它**。

    「这个工具能干什么」模型猜得出来，「它查不到什么」猜不出来 ——
    而排错路的代价（多花几步、甚至排除掉真正的原因）要大得多。
    """
    for spec in _registry().specs():
        description = spec["function"]["description"]
        assert "局限" in description, f"{spec['function']['name']} 没写局限"


def test_every_description_has_a_typical_call() -> None:
    """每个工具都要给一条**典型调用**，把参数形态摆出来。

    模型看得到 schema，但看不到「一个真实调用长什么样」——
    时间是 ISO 8601 还是 unix 时间戳？服务名是短名还是全名？
    一条例子比三段解释省字。
    """
    for spec in _registry().specs():
        name = spec["function"]["name"]
        description = spec["function"]["description"]
        assert "典型调用" in description, f"{name} 没给典型调用"
        assert f"{name}(" in description, f"{name} 的典型调用里没写出工具名"


def test_descriptions_have_no_vague_filler() -> None:
    """描述里不许出现含糊词。

    「之类」「等等」这类词在给人看的文档里没问题，给模型看就是噪声：
    它不知道边界在哪，只能靠猜。写具体值比写「等等」既省字又准确。
    """
    vague = ("之类", "等等", "等，", "等）", "一些", "若干", "大致", "可能可以")
    for spec in _registry().specs():
        description = spec["function"]["description"]
        found = [word for word in vague if word in description]
        assert found == [], f"{spec['function']['name']} 的描述里有含糊词：{found}"


def test_tool_descriptions_fit_the_prompt_budget() -> None:
    """NFR-2：说明书总量有上限。

    工具说明书写在每一次请求的提示词里，一个 30 步的诊断会原样发 30 遍。
    这条测试红了不要直接调高预算 —— 先删废话。
    """
    specs = _registry().specs()
    descriptions = sum(len(spec["function"]["description"]) for spec in specs)
    parameters = sum(
        len(prop.get("description", ""))
        for spec in specs
        for prop in spec["function"]["parameters"]["properties"].values()
    )
    total = descriptions + parameters

    assert total <= TOOL_DESCRIPTION_BUDGET_CHARS, (
        f"说明书共 {total} 字，超出预算 {TOOL_DESCRIPTION_BUDGET_CHARS}"
        f"（描述 {descriptions} + 参数 {parameters}）—— 先删废话，别调预算"
    )


def test_every_parameter_description_explains_its_value() -> None:
    """参数描述要说清取值形态 —— 它同样是提示词的一部分。"""
    for spec in _registry().specs():
        name = spec["function"]["name"]
        for param, prop in spec["function"]["parameters"]["properties"].items():
            description = prop.get("description", "")
            assert len(description) >= 8, f"{name}.{param} 的参数描述太短或缺失：{description!r}"


def test_time_parameters_say_iso_8601() -> None:
    """时间参数的格式必须写明。

    模型不知道我们收的是 ISO 8601 还是 unix 时间戳 ——
    猜错的代价是一次失败的调用 + 一次重试（都是钱）。
    """
    for spec in _registry().specs():
        name = spec["function"]["name"]
        for param, prop in spec["function"]["parameters"]["properties"].items():
            looks_like_time = "datetime" in str(prop.get("format", "")) or param in {
                "start",
                "end",
                "at",
                "since",
            }
            if looks_like_time:
                assert "ISO 8601" in prop.get("description", ""), f"{name}.{param} 没写时间格式"


# --------------------------------------------------------------------------
# FIV-14：把「猜参数」变成「报错」
# --------------------------------------------------------------------------


def test_log_levels_are_an_inline_enum() -> None:
    """日志级别必须是**内联枚举**，不能是 $ref。

    内联枚举模型一眼看得到可选值；$ref 得靠调用方解析，各家模型支持程度不一。
    """
    spec = next(s for s in _registry().specs() if s["function"]["name"] == "query_logs")
    items = spec["function"]["parameters"]["properties"]["levels"]["items"]

    assert "$ref" not in items, "级别用了 $ref —— 模型可能解析不了"
    assert items["enum"] == ["DEBUG", "INFO", "WARN", "ERROR"]


def test_invalid_log_level_is_rejected_instead_of_silently_empty() -> None:
    """写错级别要**报错**，不能静默返回空。

    ``levels=["warning"]`` 以前是这样收场的：大小写被规整成 ``WARNING``，
    和 ``WARN`` 对不上 → 过滤出 0 条 → 工具回一句「这个服务这段时间没有异常」。
    **一次拼写错误被当成了「服务是健康的」** —— 这正是本项目一路上在防的
    「静默地把错误当成结论」。现在它会变成一个 ValidationError，
    被主循环喂回给模型，模型有机会自己改对。
    """
    tool = _registry().get("query_logs")

    with pytest.raises(ValidationError):
        tool(
            service="order-service",
            start=FAULT_AT,
            end=FAULT_AT + timedelta(minutes=5),
            levels=["warning"],
        )
