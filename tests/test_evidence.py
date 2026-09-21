"""FIV-17 验收测试：证据来源校验（需求 FR-8）。

## 要防的是「结论正确、过程编造」

大模型最危险的失败模式不是答错，而是**编一个像样的理由来支持答案**：

    根因：连接池被调小
    证据：query_metrics(order-service) 显示错误率从 0% 涨到 13%

如果这次调查里**根本没调用过** `query_metrics`，这条证据就是凭空写的 ——
而它读起来完全合理，§6.1 的判分也照样给满分（判分只看根因对不对）。

**一个结论正确、过程编造的诊断，比一个明确说「我查不出来」的诊断糟糕得多。**

下面每个测试都对应这个场景的一个变体。
"""

from __future__ import annotations

import json

from doubles import ScriptedLLM, call, diagnosis_payload, submit  # type: ignore[import-not-found]
from fivewhys.agent import diagnose
from fivewhys.agent.evidence import check_evidence, evidence_sources
from fivewhys.config import Settings
from fivewhys.models import (
    Confidence,
    Diagnosis,
    Evidence,
    FaultCategory,
    ToolCallRecord,
    WhyStep,
)


def _settings(**overrides: object) -> Settings:
    """这个文件测的就是证据政策，所以用**默认值**（verify_evidence=True）。"""
    base: dict[str, object] = {"max_steps": 6, "max_cost_usd": 1.0, "trace_enabled": False}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _registry():
    from fivewhys.mock.logstore import LogStore
    from fivewhys.tools import DataSource, build_registry

    return build_registry(DataSource.logs_only(LogStore()))


def _diagnosis(*, sources: list[str], why_sources: list[list[str]] | None = None) -> Diagnosis:
    return Diagnosis(
        root_cause="连接池被调小",
        root_cause_service="order-service",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        confidence=Confidence.HIGH,
        why_chain=[
            WhyStep(
                depth=index + 1,
                question=f"为什么（第 {index + 1} 层）？",
                answer="因为…",
                evidence=[Evidence(source=source, finding="看到…") for source in group],
            )
            for index, group in enumerate(why_sources or [])
        ],
        evidence=[Evidence(source=source, finding="看到…") for source in sources],
        ruled_out=[],
        suggested_fix="回滚配置",
        summary="连接池耗尽",
    )


def _records(*tools: str) -> list[ToolCallRecord]:
    return [ToolCallRecord(step=1, tool=tool, args={}, ok=True) for tool in tools]


# --------------------------------------------------------------------------
# ⭐ 编造的证据要被抓住
# --------------------------------------------------------------------------


def test_fabricated_tool_reference_is_caught() -> None:
    """⭐ 引用了**存在但这次没调用过**的工具 → 判为编造。"""
    diagnosis = _diagnosis(sources=["query_metrics(order-service)"])

    problems = check_evidence(
        diagnosis, _records("query_logs"), known_tools=["query_logs", "query_metrics"]
    )

    assert any("编造" in problem for problem in problems), problems
    assert any("query_metrics" in problem for problem in problems)


def test_evidence_without_any_tool_name_is_caught() -> None:
    """一个工具名都没提（比如只写「日志」）→ 不合格，并且要**告诉它能引用什么**。"""
    diagnosis = _diagnosis(sources=["日志里看到连接等待变长"])

    problems = check_evidence(diagnosis, _records("query_logs"), known_tools=["query_logs"])

    assert any("看不出是哪次工具调用" in problem for problem in problems), problems
    assert any("query_logs" in problem for problem in problems), "要列出实际调用过的工具"


def test_empty_source_is_caught() -> None:
    problems = check_evidence(_diagnosis(sources=["   "]), _records("query_logs"))
    assert any("是空的" in problem for problem in problems), problems


def test_a_diagnosis_with_no_evidence_at_all_is_rejected() -> None:
    """没有证据的根因等于猜测 —— 不能因为「空集合里每条都合法」就放过去。"""
    problems = check_evidence(_diagnosis(sources=[]), _records("query_logs"))

    assert any("一条证据都没有" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# ⭐ 后门：证据藏在 why_chain 里也要查
# --------------------------------------------------------------------------


def test_evidence_inside_the_why_chain_is_checked_too() -> None:
    """⭐ FR-8 说的是「结论里的每条证据」。

    只查顶层 ``evidence`` 就等于开了一个明显的后门：
    把编造的证据塞进某一层 why 里就绕过去了。
    """
    diagnosis = _diagnosis(sources=["query_logs(order-service)"], why_sources=[["query_metrics"]])
    assert len(evidence_sources(diagnosis)) == 2

    problems = check_evidence(
        diagnosis, _records("query_logs"), known_tools=["query_logs", "query_metrics"]
    )

    assert any("编造" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# 别误伤诚实的结论
# --------------------------------------------------------------------------


def test_honest_evidence_passes() -> None:
    diagnosis = _diagnosis(sources=["query_logs(order-service)", "get_config(order-service)"])

    problems = check_evidence(
        diagnosis,
        _records("query_logs", "get_config"),
        known_tools=["query_logs", "get_config", "query_metrics"],
    )

    assert problems == []


def test_loose_wording_is_accepted() -> None:
    """措辞宽松无所谓 —— 只要提到了真调用过的工具就算诚实。

    为什么放宽：模型天然会写「日志查询显示…」「get_config 的结果表明…」，
    卡死格式只会把诚实的结论退回去重做，白烧 token。
    """
    for source in (
        "query_logs",
        "query_logs(order-service) 的结果",
        "我调用 query_logs 查了 14:02 前后的日志",
    ):
        assert check_evidence(_diagnosis(sources=[source]), _records("query_logs")) == [], source


def test_a_failed_tool_call_still_counts_as_investigated() -> None:
    """调用过但报错了，引用它仍然是**诚实**的 —— 它确实查过。"""
    records = [ToolCallRecord(step=1, tool="query_logs", args={}, ok=False, error="boom")]

    assert check_evidence(_diagnosis(sources=["query_logs(order-service)"]), records) == []


def test_we_do_not_check_parameters_on_purpose() -> None:
    """**故意不校验 source 里提到的服务名/参数。**

    因为那不可判定：「query_logs 显示 order-service 与 payment-service 都正常」
    里出现的 payment-service 可能是对比说明，不是声称查过它。
    宁可漏判，也不要误判 —— 误判会把诚实的结论退回去重做。
    """
    diagnosis = _diagnosis(sources=["query_logs：order-service 与 payment-service 都正常"])

    problems = check_evidence(diagnosis, _records("query_logs"), known_tools=["query_logs"])

    assert problems == [], "提到了没查过的服务名不该被判成编造"


# --------------------------------------------------------------------------
# ⭐ 在主循环里生效（默认配置）
# --------------------------------------------------------------------------


async def test_loop_rejects_fabricated_evidence_and_lets_the_model_fix_it() -> None:
    """⭐ 端到端：编造 → 被拒 → 喂回具体错误 → 模型改对 → 通过。"""
    llm = ScriptedLLM(
        [
            call("query_logs", {"service": "order-service"}),
            # 第一次提交：证据引用了一个从没调用过的工具
            submit(evidence=[{"source": "query_metrics(order-service)", "finding": "错误率涨了"}]),
            # 第二次提交：改成真的调用过的那个（fixture 默认就是 query_logs）
            submit(),
        ]
    )

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(),
    )

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert llm.rounds == 3, "应该多花一轮去纠正"

    # 第一次提交必须被记成失败，并且喂回去的信息要**具体**
    rejected = [record for record in run.tool_calls if record.tool == "submit_diagnosis"]
    assert len(rejected) == 1 and rejected[0].ok is False
    assert "编造" in (rejected[0].error or "")
    assert "query_logs" in (rejected[0].error or ""), "要告诉它可以引用哪些工具"


async def test_loop_gives_up_as_max_steps_when_the_model_keeps_fabricating() -> None:
    """一直编造 → 最终按 max_steps 记「错」（§6.2）。

    这是公平的：**说不清证据来自哪里，就是方法上的失败。**
    """
    fabricated = submit(
        evidence=[{"source": "query_metrics(order-service)", "finding": "编的"}],
    )
    llm = ScriptedLLM([call("query_logs", {"service": "order-service"}), *[fabricated] * 5])

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_steps=4),
    )

    assert run.stop_reason == "max_steps"
    assert run.diagnosis is None
    assert all(not record.ok for record in run.tool_calls if record.tool == "submit_diagnosis")


async def test_verification_is_on_by_default() -> None:
    """默认必须开着 —— 这条约束的价值就在于**默认生效**。"""
    assert Settings(_env_file=None).verify_evidence is True  # type: ignore[call-arg]


async def test_verification_can_be_turned_off_for_the_m7_baseline() -> None:
    """M7 要测「结构化输出约束带来多少提升」，没有基线就说不清。

    关掉之后，同样的编造证据会被放行 —— 这就是那组对照。
    """
    llm = ScriptedLLM(
        [
            call("query_logs", {"service": "order-service"}),
            submit(evidence=[{"source": "query_metrics(order-service)", "finding": "编的"}]),
        ]
    )

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(verify_evidence=False),
    )

    assert run.stop_reason == "submitted", "关掉校验后不该再拦"
    assert run.diagnosis is not None


async def test_rejection_is_visible_in_the_trace(tmp_path) -> None:
    """被拒也要留在轨迹里 —— 复盘时要能看出「它编过，被抓了」。"""
    from fivewhys.trace import TraceWriter, read_events

    llm = ScriptedLLM(
        [
            call("query_logs", {"service": "order-service"}),
            submit(evidence=[{"source": "query_metrics(order-service)", "finding": "编的"}]),
            submit(),
        ]
    )
    writer = TraceWriter("evidence-run", root=tmp_path)

    await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(),
        trace=writer,
    )

    submit_events = [
        event
        for event in read_events(writer.path)
        if event["kind"] == "tool_result" and event["tool"] == "submit_diagnosis"
    ]
    assert [event["ok"] for event in submit_events] == [False, True]
    assert "编造" in submit_events[0]["error"]


def test_the_json_round_trip_still_works() -> None:
    """守住 fixture：``diagnosis_payload`` 必须仍然是一份结构合法的结论。"""
    payload = json.loads(diagnosis_payload())
    assert payload["evidence"][0]["source"] == "query_logs(order-service)"
    assert Diagnosis.model_validate(payload)
