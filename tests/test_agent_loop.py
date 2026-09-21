"""FIV-4 验收测试：agent 主循环。

**全部测试都不需要 API Key、不联网** —— 靠 ``doubles.ScriptedLLM`` 注入预置响应。

这本身就是设计目标之一：主循环的逻辑必须能被确定性测试，
否则每跑一次单测就要联网、要花钱、结果还不稳定。

对应需求：FR-6（诊断 agent）、FR-7（终止控制）、FR-9（轨迹记录）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from doubles import ScriptedLLM, call, diagnosis_payload, say, submit
from fivewhys.agent import SUBMIT_TOOL_NAME, diagnose, submit_tool_spec
from fivewhys.config import Settings
from fivewhys.mock.logstore import LogStore
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.mock.service import MockService
from fivewhys.tools import ToolRegistry, build_registry

T0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
FAULT_AT = T0 + timedelta(minutes=5)
WINDOW = {"start": FAULT_AT.isoformat(), "end": (FAULT_AT + timedelta(minutes=5)).isoformat()}
QUESTION = "order-service 从 14:05 前后开始错误率飙升，帮忙定位一下原因"


def _registry() -> ToolRegistry:
    store = LogStore()
    service = MockService("order-service", store)
    service.normal_operation(T0, FAULT_AT)
    inject_db_pool_exhausted(store, service, FAULT_AT)
    return build_registry(store)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "max_steps": 5,
        "max_cost_usd": 1.0,
        "max_why_depth": 5,
        "temperature": 0.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _query_logs_args() -> dict[str, object]:
    return {"service": "order-service", **WINDOW}


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


async def test_submits_immediately() -> None:
    llm = ScriptedLLM([submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert run.diagnosis.root_cause_service == "order-service"
    assert run.steps == 1
    assert run.duration_s is not None and run.duration_s >= 0
    assert run.error is None


async def test_calls_tool_then_submits() -> None:
    """完整路径：先查日志，拿到结果，再下结论。"""
    llm = ScriptedLLM([call("query_logs", _query_logs_args()), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert len(run.tool_calls) == 1

    record = run.tool_calls[0]
    assert record.ok is True
    assert record.tool == "query_logs"
    assert record.step == 1
    assert record.error is None

    # 工具结果必须喂回模型 —— 否则模型无从判断
    assert "共命中" in llm.all_content()


async def test_system_prompt_tells_the_model_the_stop_condition() -> None:
    """模型必须被告知「只有 submit_diagnosis 能结束调查」，否则它会把话说完了事。"""
    llm = ScriptedLLM([submit()])
    await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )
    assert SUBMIT_TOOL_NAME in llm.all_content()
    assert QUESTION in llm.all_content()


# --------------------------------------------------------------------------
# 错误处理：错误是一种「输入」，不是终点
# --------------------------------------------------------------------------


async def test_invalid_json_arguments_are_fed_back() -> None:
    llm = ScriptedLLM([call("query_logs", "这不是 JSON"), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.tool_calls[0].ok is False
    assert "JSON" in (run.tool_calls[0].error or "")

    # 关键：循环没崩，而且把具体错误喂回去了
    assert run.stop_reason == "submitted"
    assert "不是合法的 JSON" in llm.all_content()


async def test_unknown_tool_name_is_fed_back() -> None:
    """模型可能编造工具名 —— 报错信息要能帮它纠正，而不是让诊断崩掉。"""
    llm = ScriptedLLM([call("query_metrics", {"service": "order-service"}), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.tool_calls[0].ok is False
    assert "未知工具" in (run.tool_calls[0].error or "")
    assert run.stop_reason == "submitted"
    assert "未知工具" in llm.all_content()


async def test_tool_exception_is_fed_back() -> None:
    """工具内部抛异常（例如参数校验失败）也不能让诊断崩掉。"""
    llm = ScriptedLLM(
        [call("query_logs", {"service": "order-service"}), submit()]  # 缺 start / end
    )
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.tool_calls[0].ok is False
    assert "工具执行失败" in (run.tool_calls[0].error or "")
    assert run.stop_reason == "submitted"


async def test_invalid_diagnosis_is_rejected_and_retried() -> None:
    """提交的结论字段不全时，把校验错误喂回去让它自己修正。"""
    broken = json.loads(diagnosis_payload())
    del broken["root_cause_service"]  # 去掉一个必填字段

    llm = ScriptedLLM([call(SUBMIT_TOOL_NAME, broken, call_id="bad_1"), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert run.tool_calls[0].ok is False
    assert "字段结构" in (run.tool_calls[0].error or "")
    assert "格式不合法" in llm.all_content()


async def test_malformed_json_submission_is_fed_back() -> None:
    """模型交回一段根本不是 JSON 的东西时，也要喂回错误让它重交。"""
    llm = ScriptedLLM([call(SUBMIT_TOOL_NAME, "这不是 JSON，我直接说吧"), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert run.tool_calls[0].ok is False
    assert "字段结构" in (run.tool_calls[0].error or "")


async def test_plain_text_triggers_a_nudge_instead_of_ending() -> None:
    """模型只顾说话不调工具时，要推它一把，而不是当作调查结束。"""
    llm = ScriptedLLM([say("我先看看日志。"), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert llm.rounds == 2, "应该再问一轮，而不是直接结束"
    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None


# --------------------------------------------------------------------------
# 终止条件（FR-7）
# --------------------------------------------------------------------------


async def test_max_steps_stops_the_loop() -> None:
    llm = ScriptedLLM([call("query_logs", _query_logs_args()) for _ in range(10)])
    run = await diagnose(
        scenario_id="s1",
        question=QUESTION,
        registry=_registry(),
        llm=llm,
        settings=_settings(max_steps=3),
    )

    assert run.stop_reason == "max_steps"
    assert run.diagnosis is None
    assert run.steps == 3
    assert llm.rounds == 3


async def test_max_cost_stops_the_loop() -> None:
    llm = ScriptedLLM([call("query_logs", _query_logs_args(), cost_usd=0.5) for _ in range(10)])
    run = await diagnose(
        scenario_id="s1",
        question=QUESTION,
        registry=_registry(),
        llm=llm,
        settings=_settings(max_steps=10, max_cost_usd=0.6),
    )

    assert run.stop_reason == "max_cost"
    assert run.diagnosis is None
    assert run.total_cost_usd > 0.6


async def test_llm_exception_is_recorded_as_error() -> None:
    """崩溃要记录成结果，而不是抛给调用方 —— 需求 §6.2 要单独统计崩溃率。"""
    llm = ScriptedLLM([RuntimeError("connection reset by peer")])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.stop_reason == "error"
    assert run.error is not None and "connection reset" in run.error
    assert run.diagnosis is None
    assert run.finished_at is not None


# --------------------------------------------------------------------------
# 轨迹与成本（FR-9 / NFR-2）
# --------------------------------------------------------------------------


async def test_run_records_tokens_and_cost() -> None:
    llm = ScriptedLLM(
        [
            call(
                "query_logs",
                _query_logs_args(),
                cost_usd=0.01,
                prompt_tokens=100,
                completion_tokens=50,
            ),
            submit(cost_usd=0.02, prompt_tokens=200, completion_tokens=80),
        ]
    )
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.total_tokens == 100 + 50 + 200 + 80
    assert abs(run.total_cost_usd - 0.03) < 1e-9
    assert run.steps == 2


async def test_tool_latency_is_recorded() -> None:
    llm = ScriptedLLM([call("query_logs", _query_logs_args()), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )
    assert run.tool_calls[0].latency_ms >= 0


# --------------------------------------------------------------------------
# submit_diagnosis 的工具定义
# --------------------------------------------------------------------------


def test_submit_spec_has_no_refs() -> None:
    """$ref / $defs 要就地展开 —— 部分 provider 的 function calling 不支持。"""
    spec = submit_tool_spec()
    serialized = json.dumps(spec)
    assert "$ref" not in serialized
    assert "$defs" not in serialized

    params = spec["function"]["parameters"]
    assert {"root_cause", "root_cause_service", "fault_category", "why_chain"} <= set(
        params["properties"]
    )


def test_submit_spec_describes_it_as_the_only_way_to_finish() -> None:
    description = submit_tool_spec()["function"]["description"]
    assert "唯一" in description


def test_all_requirement_fields_are_required() -> None:
    """需求 FR-6 列的字段必须全部在 required 里。

    一旦某个字段带了默认值，它就不在 required 里，模型可以整段省略，
    最后交上来的只剩一句根因 —— 「结构化输出」就名存实亡了。
    这个坑是 FIV-5 的起飞前检查发现的（当时 required 只有 4 个）。
    """
    required = set(submit_tool_spec()["function"]["parameters"]["required"])
    assert required == {
        "root_cause",
        "root_cause_service",
        "fault_category",
        "confidence",
        "why_chain",
        "evidence",
        "ruled_out",
        "suggested_fix",
        "summary",
    }


def test_submit_spec_requires_all_nine_fields() -> None:
    """防止将来有人给某个字段加回默认值，悄悄把它从 required 里挤出去。"""
    required = submit_tool_spec()["function"]["parameters"]["required"]
    assert len(required) == 9


# --------------------------------------------------------------------------
# _inline_refs — 它错了，整个 submit_diagnosis 就没法用
# --------------------------------------------------------------------------


def test_inline_refs_expands_reference() -> None:
    from fivewhys.agent.loop import _inline_refs

    schema = {
        "$defs": {"Thing": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Thing"}},
    }
    result = _inline_refs(schema)

    assert "$defs" not in result
    assert result["properties"]["item"]["type"] == "object"
    assert "a" in result["properties"]["item"]["properties"]


def test_inline_refs_keeps_sibling_keys() -> None:
    """``$ref`` 旁边还挂着别的键（例如 description）时，展开后必须保留。"""
    from fivewhys.agent.loop import _inline_refs

    schema = {
        "$defs": {"Thing": {"type": "object"}},
        "properties": {"item": {"$ref": "#/$defs/Thing", "description": "一个东西"}},
    }
    result = _inline_refs(schema)

    assert result["properties"]["item"]["description"] == "一个东西"
    assert result["properties"]["item"]["type"] == "object"


def test_inline_refs_handles_nested_arrays() -> None:
    from fivewhys.agent.loop import _inline_refs

    schema = {
        "$defs": {"Thing": {"type": "object"}},
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Thing"}}},
    }
    result = _inline_refs(schema)

    assert result["properties"]["items"]["items"]["type"] == "object"
    assert "$ref" not in json.dumps(result)


async def test_json_array_arguments_are_rejected() -> None:
    """模型偶尔会返回合法 JSON 但不是对象（例如数组）—— 也要兜住。"""
    llm = ScriptedLLM([call("query_logs", "[1, 2, 3]"), submit()])
    run = await diagnose(
        scenario_id="s1", question=QUESTION, registry=_registry(), llm=llm, settings=_settings()
    )

    assert run.tool_calls[0].ok is False
    assert "必须是 JSON 对象" in (run.tool_calls[0].error or "")
    assert run.stop_reason == "submitted", "循环不能因此崩掉"
