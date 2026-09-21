"""FIV-13 验收测试：4 个工具 + 五件工具能不能真的走出证据链。

对应需求：FR-5（排障工具）、NFR-11（上下文预算）

这一层的测试分三段：

1. **每个工具单独**：正常 / 空结果 / 参数越界
2. **场景感知**：服务名写错时会不会把笔误当成「没问题」
3. **证据链**（最重要）：五个工具串起来，能不能从「错误率飙升」
   一路走到「配置被改小了」—— 而日志里从头到尾没有答案

第 3 段才是这个任务真正的验收标准：工具能返回数据不算完成，
**agent 靠这些数据能推出根因**才算。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fivewhys.mock.injectors import available
from fivewhys.models import FaultCategory
from fivewhys.scenario import Scenario, build_scenario
from fivewhys.tools import DataSource, ToolRegistry, build_registry

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
# build_scenario 默认：先跑 2 分钟正常流量，故障从 14:02 开始
FAULT_AT = T0 + timedelta(minutes=2)
WINDOW_END = FAULT_AT + timedelta(minutes=5)


@pytest.fixture(scope="module")
def db_pool_scenario() -> Scenario:
    return build_scenario(FaultCategory.DB_POOL_EXHAUSTED, seed=0, base_time=T0)


@pytest.fixture(scope="module")
def registry(db_pool_scenario: Scenario) -> ToolRegistry:
    return build_registry(DataSource.from_scenario(db_pool_scenario))


# --------------------------------------------------------------------------
# query_metrics
# --------------------------------------------------------------------------


def test_metrics_lists_buckets_and_summarises(registry: ToolRegistry) -> None:
    out = registry.get("query_metrics")(service="order-service", start=T0, end=WINDOW_END)

    assert "共" in out and "个时间桶" in out
    assert "错误率最高" in out
    assert "P95 最高" in out
    assert "最早出现错误的时间桶" in out


def test_metrics_finds_when_it_started_failing(registry: ToolRegistry) -> None:
    """工具要替模型把「从什么时候开始坏的」算出来 —— 这正是人要花力气看的。"""
    out = registry.get("query_metrics")(service="order-service", start=T0, end=WINDOW_END)
    summary = out.splitlines()[0]

    assert (
        f"{FAULT_AT:%H:%M:%S}" in summary
        or f"{FAULT_AT + timedelta(minutes=1):%H:%M:%S}" in summary
    )


def test_metrics_of_a_quiet_window_reports_no_problem(registry: ToolRegistry) -> None:
    """正常时段（故障前）必须显示 0 错误 —— 否则「基线」这个概念就不存在了。

    窗口终点要比故障早 1 秒：时间桶是**左闭右闭**的，故障正好发生在 14:02:00，
    如果窗口到 14:02:00，那个桶里会混进故障采样，看起来像「故障前就在报错」。
    这个边界效应是真实的监控系统里也有的（桶会跨越故障起点），
    也正是工具要在摘要里给出「最早出现错误的时间桶」而不是「故障开始时间」的原因。
    """
    out = registry.get("query_metrics")(
        service="order-service", start=T0, end=FAULT_AT - timedelta(seconds=1)
    )
    assert "错误率 0%" in out
    assert "全部时间桶都没有失败请求" in out


def test_metrics_bucket_granularity_changes_the_row_count(registry: ToolRegistry) -> None:
    tool = registry.get("query_metrics")
    fine = tool(service="order-service", start=T0, end=WINDOW_END, bucket_seconds=30)
    coarse = tool(service="order-service", start=T0, end=WINDOW_END, bucket_seconds=300)

    assert fine.splitlines()[0].split("，")[0] != coarse.splitlines()[0].split("，")[0]


def test_metrics_empty_window_gives_a_next_step(registry: ToolRegistry) -> None:
    """服务名对、窗口不对 —— 提示要指向「时间窗口」而不是「服务名」。"""
    out = registry.get("query_metrics")(
        service="order-service",
        start=T0 - timedelta(days=3),
        end=T0 - timedelta(days=3) + timedelta(minutes=1),
    )
    assert "没有任何请求采样" in out
    assert "时间窗口不对" in out


def test_metrics_unknown_service_lists_the_real_ones(registry: ToolRegistry) -> None:
    out = registry.get("query_metrics")(service="order-svc", start=T0, end=WINDOW_END)
    assert "没有服务" in out
    assert "payment-service" in out


def test_metrics_rejects_a_reversed_window(registry: ToolRegistry) -> None:
    out = registry.get("query_metrics")(service="order-service", start=WINDOW_END, end=T0)
    assert "参数有误" in out


def test_metrics_of_a_healthy_scenario_says_so(base_time: datetime = T0) -> None:
    healthy = build_scenario(FaultCategory.NO_FAULT, seed=0, base_time=base_time)
    tool = build_registry(DataSource.from_scenario(healthy)).get("query_metrics")

    out = tool(service="order-service", start=T0, end=WINDOW_END)
    assert "全部时间桶都没有失败请求" in out or "错误率 0%" in out


# --------------------------------------------------------------------------
# get_config —— 答案是这里
# --------------------------------------------------------------------------


def test_config_shows_the_change_that_caused_it(registry: ToolRegistry) -> None:
    """⭐ 这个是本项目的「啊哈」：配置里能看到 `db.pool_size: 50 -> 5`。

    注意这条信息**在日志里根本不存在**（日志只说 config reloaded）——
    它只能靠「怀疑到配置变过，然后来查」才能拿到。
    """
    out = registry.get("get_config")(
        service="order-service",
        since=FAULT_AT - timedelta(minutes=5),
        at=WINDOW_END,
    )

    assert "db.pool_size" in out
    assert "50 -> 5" in out


def test_config_without_since_still_finds_it(registry: ToolRegistry) -> None:
    out = registry.get("get_config")(service="order-service")
    assert "50 -> 5" in out


def test_config_since_filters_older_changes(registry: ToolRegistry) -> None:
    """since 之后没有变化时，必须明说 0 次 —— 这是排除性证据。"""
    out = registry.get("get_config")(
        service="order-service",
        since=FAULT_AT + timedelta(minutes=1),
    )
    assert "没有任何配置变化" in out
    assert "50 -> 5" not in out


def test_config_can_omit_the_effective_values(registry: ToolRegistry) -> None:
    out = registry.get("get_config")(service="order-service", include_values=False)
    assert "生效的配置" not in out
    assert "db.pool_size" in out, "变化本身仍然要给出"


def test_config_shows_effective_values_by_default(registry: ToolRegistry) -> None:
    """只给变化是不够的 —— 模型还需要知道「现在是什么样」。"""
    out = registry.get("get_config")(service="order-service")
    assert "生效的配置" in out
    assert "http.slo_ms" in out


def test_config_of_a_healthy_scenario_reports_no_change() -> None:
    """健康场景里配置没动过 —— 工具必须如实说 0 次，不能编。"""
    healthy = build_scenario(FaultCategory.NO_FAULT, seed=0, base_time=T0)
    tool = build_registry(DataSource.from_scenario(healthy)).get("get_config")

    out = tool(service="order-service")
    assert "0 次配置变化" in out
    assert "没有任何配置变化" in out


def test_config_unknown_service_is_reported(registry: ToolRegistry) -> None:
    out = registry.get("get_config")(service="order")
    assert "没有服务" in out
    assert "inventory-service" in out


def test_config_rejects_since_after_at(registry: ToolRegistry) -> None:
    out = registry.get("get_config")(service="order-service", since=WINDOW_END, at=FAULT_AT)
    assert "参数有误" in out


def test_config_for_a_service_without_snapshots_is_honest() -> None:
    """服务存在、但没有配置快照时，要说清「这条路径查不到」而不是「配置没问题」。

    这两句话差别很大：前者让 agent 换一条证据继续查，
    后者会让它直接把配置排除掉 —— 而它其实什么都没查到。
    """
    from fivewhys.mock.changes import ConfigStore
    from fivewhys.tools.get_config import build_get_config_tool

    tool = build_get_config_tool(ConfigStore(), services=["order-service", "ghost-service"])
    out = tool(service="ghost-service")

    assert "0 条配置快照" in out
    assert "别把它当成「配置没问题」" in out


def test_config_truncates_a_long_output() -> None:
    """变化太多 / 配置项太多时要有明确的截断提示 —— 输出不能悄悄膨胀（NFR-11）。"""
    from fivewhys.mock.changes import ConfigStore
    from fivewhys.tools.get_config import MAX_CHANGES, MAX_ITEMS, build_get_config_tool

    configs = ConfigStore()
    values = {f"key{i:03d}": i for i in range(MAX_ITEMS + 5)}
    configs.record_values(T0, "order-service", values)
    for step in range(MAX_CHANGES + 1):
        key = f"key{step:03d}"
        values = {**values, key: values[key] + 1}
        configs.record_values(T0 + timedelta(seconds=step + 1), "order-service", values)

    out = build_get_config_tool(configs, services=["order-service"])(service="order-service")

    assert "变化过多" in out
    assert "配置项过多" in out
    assert f"只显示前 {MAX_CHANGES} 条" in out
    assert f"只显示前 {MAX_ITEMS} 项" in out


# --------------------------------------------------------------------------
# get_deploy_history
# --------------------------------------------------------------------------


def test_deploy_history_lists_all_services_when_asked(registry: ToolRegistry) -> None:
    out = registry.get("get_deploy_history")()
    assert "全部服务" in out
    for service in ("order-service", "payment-service", "inventory-service"):
        assert service in out


def test_deploy_history_filters_by_service(registry: ToolRegistry) -> None:
    out = registry.get("get_deploy_history")(service="payment-service")
    assert "payment-service" in out
    assert "inventory-service" not in out


def test_deploy_history_can_exclude_the_deploy_as_a_suspect(registry: ToolRegistry) -> None:
    """窗口内没有发布 → 这是一条**排除性证据**，工具要把它说成结论。"""
    out = registry.get("get_deploy_history")(
        service="order-service",
        start=FAULT_AT,
        end=WINDOW_END,
    )
    assert "没有发布" in out
    assert "不是发布引起的" in out


def test_deploy_history_unknown_service_is_reported(registry: ToolRegistry) -> None:
    out = registry.get("get_deploy_history")(service="billing-service")
    assert "没有服务" in out
    assert "order-service" in out


def test_deploy_history_rejects_a_reversed_window(registry: ToolRegistry) -> None:
    out = registry.get("get_deploy_history")(start=WINDOW_END, end=T0)
    assert "参数有误" in out


# --------------------------------------------------------------------------
# get_dependencies
# --------------------------------------------------------------------------


def test_dependencies_returns_the_whole_graph(registry: ToolRegistry) -> None:
    out = registry.get("get_dependencies")()
    assert "order-service -> payment-service, inventory-service" in out
    assert "payment-service -> （没有下游）" in out


def test_dependencies_of_a_service_shows_both_directions(registry: ToolRegistry) -> None:
    """两个方向都要给：下游用来追根因，上游用来评估影响面。"""
    out = registry.get("get_dependencies")(service="payment-service")

    assert "下游" in out and "没有 —— 它是叶子服务" in out
    assert "上游" in out and "order-service" in out


def test_dependencies_marks_the_entry_service(registry: ToolRegistry) -> None:
    out = registry.get("get_dependencies")(service="order-service")
    assert "入口服务" in out


def test_dependencies_teaches_how_to_use_it(registry: ToolRegistry) -> None:
    """光给「上游/下游」两个词不够，要顺手说清「那我现在该往哪查」。"""
    out = registry.get("get_dependencies")(service="order-service")
    assert "根因可能在" in out
    assert "payment-service" in out


def test_dependencies_unknown_service_lists_the_real_ones(registry: ToolRegistry) -> None:
    out = registry.get("get_dependencies")(service="order")
    assert "没有服务" in out


def test_dependencies_of_a_single_service_scenario() -> None:
    """M1 的最小场景没有拓扑 —— 要说清「不用跨服务追」，而不是报错。"""
    from fivewhys.mock.logstore import LogStore

    tool = build_registry(DataSource.logs_only(LogStore())).get("get_dependencies")
    assert "没有拓扑数据" in tool()
    assert "单服务" in tool()


# --------------------------------------------------------------------------
# ⭐ 证据链：五件工具能不能真的走出根因
# --------------------------------------------------------------------------


def test_five_tools_walk_the_whole_evidence_chain(db_pool_scenario: Scenario) -> None:
    """一次完整的离线排障，只用手上的 5 个工具，且不调用 LLM。

    这正是 agent 在真实评测里要走的路：

    ::

        指标：错误率飙升            <- 从哪个时间点开始
          -> 日志：deadline exceeded  <- 症状长什么样
            -> 配置：pool_size 50->5  <- 根因在这一行
              -> 发布：没有发布         <- 排除掉「是发布引起的」
                -> 拓扑：order 是入口   <- 确认影响面

    每一步的输出都必须真的支撑下一步，否则 agent 就是在猜。
    """
    registry = build_registry(DataSource.from_scenario(db_pool_scenario))
    truth = db_pool_scenario.ground_truth

    # 1. 指标：发现异常，并给出异常开始的时间
    metrics_out = registry.get("query_metrics")(service="order-service", start=T0, end=WINDOW_END)
    assert "最早出现错误的时间桶" in metrics_out
    assert "错误率最高" in metrics_out

    # 2. 日志：异常时间点上的具体症状
    logs_out = registry.get("query_logs")(service="order-service", start=FAULT_AT, end=WINDOW_END)
    assert "deadline exceeded" in logs_out
    assert "connection wait time" in logs_out

    # 3. 配置：根因（只有走到这一步才拿得到）
    config_out = registry.get("get_config")(
        service=truth.root_cause_service, since=T0, at=WINDOW_END
    )
    assert "db.pool_size" in config_out and "50 -> 5" in config_out

    # 4. 发布：排除嫌疑（同一时间窗内没有发布）
    deploy_out = registry.get("get_deploy_history")(
        service=truth.root_cause_service, start=FAULT_AT, end=WINDOW_END
    )
    assert "不是发布引起的" in deploy_out

    # 5. 拓扑：确认这是入口服务，影响面覆盖整条链路
    deps_out = registry.get("get_dependencies")(service=truth.root_cause_service)
    assert "入口服务" in deps_out


def test_the_answer_is_only_in_the_config(db_pool_scenario: Scenario) -> None:
    """**设计纪律的机器化表述**：答案词出现在配置里，绝不出现在日志里。

    这一条同时守住两件事：
    - FR-2（日志不得泄漏答案）：日志里搜不到答案词
    - 「答案在配置里」：配置变化里确实有答案词

    如果哪天有人为了让 agent「好查一点」把 pool_size 写进日志，
    这个测试会红 —— 因为那样场景就没有难度了。
    """
    registry = build_registry(DataSource.from_scenario(db_pool_scenario))
    truth = db_pool_scenario.ground_truth
    assert truth.answer_keywords, "这个场景应该是有答案词的"

    logs_out = registry.get("query_logs")(
        service="order-service",
        start=T0,
        end=WINDOW_END,
        levels=["INFO", "WARN", "ERROR"],
        limit=200,
    )
    config_out = registry.get("get_config")(service="order-service")

    logged = [kw for kw in truth.answer_keywords if kw.lower() in logs_out.lower()]
    in_config = [kw for kw in truth.answer_keywords if kw.lower() in config_out.lower()]

    assert logged == [], f"日志里泄漏了答案：{logged}"
    assert in_config, "答案必须能在配置里查到，否则这个场景无解"


def test_every_registered_scenario_can_be_inspected_by_all_five_tools() -> None:
    """每个场景（含健康场景）都要能被 5 个工具查一遍而不报错、不抛异常。

    这是「工具层的冒烟测试」：新增一种故障时，如果它让某个工具崩了，
    这里会立刻发现 —— 而不是等到 M6 评测时才看到一堆 error。
    """
    for category in available():
        scenario = build_scenario(category, seed=0, base_time=T0)
        registry = build_registry(DataSource.from_scenario(scenario))
        service = scenario.ground_truth.root_cause_service

        assert registry.get("query_metrics")(service=service, start=T0, end=WINDOW_END)
        assert registry.get("query_logs")(service=service, start=T0, end=WINDOW_END)
        assert registry.get("get_config")(service=service)
        assert registry.get("get_deploy_history")(service=service)
        assert registry.get("get_dependencies")(service=service)


def test_no_tool_output_blows_the_context_budget(db_pool_scenario: Scenario) -> None:
    """NFR-11：任何一次工具调用的返回都不能突破预算。

    故意用最宽的参数去问（大窗口、高 limit、所有级别），
    确认预算这道闸门在最坏情况下也守得住。
    """
    from fivewhys.tools._render import MAX_RESPONSE_CHARS

    registry = build_registry(DataSource.from_scenario(db_pool_scenario))
    widest = {
        "query_logs": {
            "service": "order-service",
            "start": T0,
            "end": WINDOW_END,
            "levels": ["INFO", "WARN", "ERROR"],
            "limit": 200,
        },
        "query_metrics": {
            "service": "order-service",
            "start": T0,
            "end": WINDOW_END,
            "bucket_seconds": 10,
        },
        "get_config": {"service": "order-service", "include_values": True},
        "get_deploy_history": {"limit": 40},
        "get_dependencies": {},
    }

    for name, args in widest.items():
        out = registry.get(name)(**args)
        assert len(out) <= MAX_RESPONSE_CHARS + 200, f"{name} 的返回突破了预算：{len(out)} 字符"
