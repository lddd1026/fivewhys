"""FIV-2 验收测试：`query_logs` 的过滤、排序、预算与空结果处理。

对应需求：FR-5（排障工具）、NFR-11（上下文预算 ≤2000 token）
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.mock.service import MockService
from fivewhys.tools.query_logs import MAX_RESPONSE_CHARS, build_query_logs_tool

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
FAULT_AT = T0 + timedelta(minutes=10)
FAULT_END = FAULT_AT + timedelta(minutes=5)


def _store() -> LogStore:
    """建一个「10 分钟正常 + 5 分钟故障」的场景，另加一个无关服务。"""
    store = LogStore()
    order = MockService("order-service", store)
    order.normal_operation(T0, FAULT_AT)

    # 另一个服务，用来验证 service 过滤真的生效
    payment = MockService("payment-service", store, seed=99)
    payment.normal_operation(T0, FAULT_END)

    inject_db_pool_exhausted(store, order, FAULT_AT)
    return store


def _query(store: LogStore, **overrides: object) -> str:
    args: dict[str, object] = {
        "service": "order-service",
        "start": FAULT_AT,
        "end": FAULT_END,
        "levels": ["WARN", "ERROR"],
        "keyword": None,
        "limit": 50,
    }
    args.update(overrides)
    return build_query_logs_tool(store)(**args)


def _data_lines(output: str) -> list[str]:
    """剥掉头部，返回真正的日志行。"""
    lines = output.splitlines()
    idx = next(i for i, line in enumerate(lines) if line.startswith("服务="))
    return lines[idx + 1 :]


# --------------------------------------------------------------------------
# 基本过滤
# --------------------------------------------------------------------------


def test_default_levels_only_return_warn_and_error() -> None:
    out = _query(_store())
    data = _data_lines(out)
    assert data, "故障期间应该有 WARN/ERROR 日志"
    assert all(line[9:14].strip() in {"WARN", "ERROR"} for line in data), data[:3]
    assert "INFO" not in out.split("服务=")[-1]


def test_filters_by_service() -> None:
    """payment-service 的日志不应出现在 order-service 的查询结果里。"""
    out = _query(_store())
    assert "payment-service" not in out


def test_filters_by_time_window() -> None:
    """窗口落在故障之前时，应查不到 WARN/ERROR。"""
    out = _query(_store(), start=T0, end=T0 + timedelta(minutes=5))
    assert "共命中 0 条" in out


def test_filters_by_keyword() -> None:
    out = _query(_store(), keyword="connection wait")
    data = _data_lines(out)
    assert data
    assert all("connection wait" in line.lower() for line in data)


def test_keyword_is_case_insensitive() -> None:
    """大小写不应影响匹配结果（只比较日志行，头部会原样回显关键字）。"""
    lower = _data_lines(_query(_store(), keyword="connection wait"))
    upper = _data_lines(_query(_store(), keyword="CONNECTION WAIT"))
    assert lower, "应该匹配到 connection wait 日志"
    assert lower == upper


def test_result_contains_trace_id() -> None:
    """关键线索必须带 trace —— 这是 agent 关联同一次请求的唯一依据。"""
    out = _query(_store(), keyword="connection wait")
    assert "trace=" in out


def test_can_follow_a_trace_id() -> None:
    """工具描述教模型「用 trace_id 当 keyword 追同一次请求」，那就必须真的能查到。

    回归测试：这个 bug 是**手工验证**发现的 ——
    单元测试只覆盖了 message 匹配，从没试过用 trace_id 查，
    而 trace_id 是独立字段，原先的过滤条件根本没看它。
    """
    store = _store()
    first = _data_lines(_query(store, limit=50))
    trace_ids = [line.split("trace=")[1].strip() for line in first if "trace=" in line]

    counts = Counter(trace_ids)
    shared = [tid for tid, n in counts.items() if n >= 2]
    assert shared, "场景里应该存在共享同一 trace 的多条日志"
    trace_id = shared[0]

    data = _data_lines(_query(store, keyword=trace_id, levels=["INFO", "WARN", "ERROR"], limit=50))
    assert data, f"应该能用 trace_id={trace_id} 查到日志"
    assert all(trace_id in line for line in data)

    levels = {line[9:14].strip() for line in data}
    assert len(levels) >= 2, f"同一次请求应同时包含症状和线索，实际只有 {levels}"


# --------------------------------------------------------------------------
# 排序与头部
# --------------------------------------------------------------------------


def test_results_are_sorted_by_time() -> None:
    data = _data_lines(_query(_store()))
    stamps = [line[:8] for line in data]
    assert stamps == sorted(stamps)


def test_header_reports_total_count() -> None:
    store = _store()
    out = _query(store, limit=3)
    assert "共命中" in out
    # 摘要说命中多少条；「给你看了几条」由截断提示交代（limit=3）
    assert "limit=3" in out
    assert "被截断" in out
    assert "limit=3" in out
    assert len(_data_lines(out)) == 3


# --------------------------------------------------------------------------
# 边界情况
# --------------------------------------------------------------------------


def test_empty_result_is_explained_as_a_clue() -> None:
    """空结果必须明确说「这是线索」，否则模型会以为工具坏了。"""
    out = _query(_store(), start=T0, end=T0 + timedelta(minutes=1))
    assert "共命中 0 条" in out
    assert "线索" in out


def test_reversed_window_returns_parameter_error() -> None:
    out = _query(_store(), start=FAULT_END, end=FAULT_AT)
    assert "参数有误" in out


def test_char_budget_is_respected() -> None:
    """NFR-11：单次返回必须受预算限制，不能把几万行塞进上下文。"""
    data = _data_lines(_query(_store(), start=T0, end=FAULT_END, levels=["INFO", "WARN", "ERROR"]))
    total_chars = len("\n".join(data))
    assert total_chars <= MAX_RESPONSE_CHARS, f"返回 {total_chars} 字符，超出预算"


def test_whole_response_fits_the_budget() -> None:
    """⭐ 预算管的是**整段返回**，不是只有明细行。

    上线前审查实测到的越界：原实现只在明细行上卡预算，表头（摘要 + 元信息 +
    截断提示）是加在预算之外的 —— 最宽的参数下整段返回 6108 字符，
    而 NFR-11 说的是「单次工具调用返回 ≤ 2000 token（约 6000 字符）」。
    """
    out = _query(_store(), start=T0, end=FAULT_END, levels=["INFO", "WARN", "ERROR"], limit=200)
    assert len(out) <= MAX_RESPONSE_CHARS, f"整段返回 {len(out)} 字符，超出预算"


def test_budget_truncation_is_disclosed() -> None:
    """被预算截断时必须告知模型，否则它会以为这就是全部。"""
    out = _query(_store(), start=T0, end=FAULT_END, levels=["INFO", "WARN", "ERROR"], limit=200)
    assert "共命中" in out
    assert "被截断" in out
    assert "limit=200" in out
    assert "6000" in out, "要说明预算数字，模型才知道该怎么缩"


def test_small_window_returns_full_content_without_truncation_note() -> None:
    out = _query(_store(), keyword="connection wait", limit=200)
    assert "被截断" not in out
