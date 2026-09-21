"""FIV-18 验收测试：终止条件补齐（需求 FR-7）。

## `max_why_depth` 的准确含义

FR-7 把「达到 5 Whys 深度上限」列为**主要停止条件**，但用 tool calling 时
模型在提交前不会暴露自己追问到第几层 —— 循环**没办法**在提交之前拦住它。

能做的、也做了的是：**提交时看链条有多长**，据此区分两种「停」：

============================  =====================  ==========================
链长                           stop_reason            含义
============================  =====================  ==========================
< 上限                         ``submitted``           它认为追到底了（提前收敛）
≥ 上限                         ``max_why_depth``       它是被追问预算停住的
============================  =====================  ==========================

需求 §6.2 对两者**判分完全一样** —— 这个标签只影响怎么解释数字，不影响准确率。
下面既测这个区分，也测「判分一样」这条不变量（不然标签就悄悄改变了指标）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from doubles import ScriptedLLM, diagnosis_payload, submit  # type: ignore[import-not-found]
from fivewhys.agent import diagnose
from fivewhys.agent.evidence import check_evidence
from fivewhys.config import Settings
from fivewhys.models import Diagnosis, FaultCategory, GroundTruth, ToolCallRecord
from fivewhys.scoring import score_diagnosis
from fivewhys.trace import summarize


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "max_steps": 6,
        "max_cost_usd": 1.0,
        "trace_enabled": False,
        "verify_evidence": False,  # 这个文件测停止条件，不测证据政策
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _registry():
    from fivewhys.mock.logstore import LogStore
    from fivewhys.tools import DataSource, build_registry

    return build_registry(DataSource.logs_only(LogStore()))


def _why_chain(depth: int) -> list[dict[str, object]]:
    return [
        {"depth": index + 1, "question": f"为什么第 {index + 1} 层？", "answer": "因为…"}
        for index in range(depth)
    ]


async def _run_with_depth(depth: int, *, max_why_depth: int = 5):
    llm = ScriptedLLM([submit(why_chain=_why_chain(depth))])
    return await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_why_depth=max_why_depth),
    )


# --------------------------------------------------------------------------
# ⭐ 两种「停」要能分辨
# --------------------------------------------------------------------------


async def test_submitting_at_the_depth_cap_is_recorded_as_max_why_depth() -> None:
    """⭐ 顶到上限 → `max_why_depth`：它是**被预算停住**的，不是自然到底。"""
    run = await _run_with_depth(depth=5)

    assert run.stop_reason == "max_why_depth"
    assert run.diagnosis is not None
    assert len(run.diagnosis.why_chain) == 5


async def test_submitting_below_the_cap_is_recorded_as_submitted() -> None:
    """没顶到 → `submitted`：它自己认为追到底了。"""
    run = await _run_with_depth(depth=3)

    assert run.stop_reason == "submitted"
    assert len(run.diagnosis.why_chain) == 3


async def test_the_cap_is_configurable() -> None:
    """上限调成 3 之后，3 层就算「顶到」。"""
    run = await _run_with_depth(depth=3, max_why_depth=3)
    assert run.stop_reason == "max_why_depth"

    # 同一个深度，上限 5 时就不算顶到 —— 证明判定真的用了配置，不是写死的 5
    run = await _run_with_depth(depth=3, max_why_depth=5)
    assert run.stop_reason == "submitted"


async def test_a_deeper_chain_than_the_cap_still_counts_as_max_why_depth() -> None:
    """模型不听话、追了 7 层（超过上限）：仍然算「顶到」，且**不因此判死**。

    超过上限是提示词层面的不服从，不是结论错误 —— 为它丢掉一份正确的诊断得不偿失。
    """
    run = await _run_with_depth(depth=7)

    assert run.stop_reason == "max_why_depth"
    assert len(run.diagnosis.why_chain) == 7


async def test_an_empty_why_chain_is_still_submitted() -> None:
    """一条追问都没有（0 层）：不崩、不判 max_why_depth。

    追得浅是**准确率**问题（M6 会算出来），不是正确性问题。
    """
    run = await _run_with_depth(depth=0)

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert run.diagnosis.why_chain == []


# --------------------------------------------------------------------------
# ⭐ 判分不受这个标签影响（§6.2 的不变量）
# --------------------------------------------------------------------------


def test_the_label_does_not_change_the_score() -> None:
    """⭐ §6.2 说 `submitted` 和 `max_why_depth` 都按 §6.1 判 —— 必须真的如此。

    如果哪天有人「顺手」让 max_why_depth 扣分，准确率就会被一个**解释性标签**
    悄悄改掉。这条测试就是钉住这一点。
    """
    truth = GroundTruth(
        scenario_id="s",
        fault_category=FaultCategory.DB_POOL_EXHAUSTED,
        root_cause_service="order-service",
        root_cause="连接池被调小",
        injected_at=datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
        symptoms=[],
        match_keywords=["pool"],
    )

    payload = json.loads(diagnosis_payload())
    deep = Diagnosis.model_validate({**payload, "why_chain": _why_chain(5)})
    shallow = Diagnosis.model_validate({**payload, "why_chain": _why_chain(2)})

    # 判分函数只看结论内容，压根不该知道 stop_reason 是什么
    assert score_diagnosis(deep, truth).total == score_diagnosis(shallow, truth).total


# --------------------------------------------------------------------------
# 报表与轨迹要能看见层数
# --------------------------------------------------------------------------


async def test_why_depth_is_visible_in_the_trace_summary(tmp_path) -> None:
    """M6 的「平均追问层数」直接取这个数。"""
    from fivewhys.trace import TraceWriter

    writer = TraceWriter("depth-run", root=tmp_path)
    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=ScriptedLLM([submit(why_chain=_why_chain(4))]),
        settings=_settings(),
        trace=writer,
    )

    info = summarize(writer.path)
    assert info["why_depth"] == 4
    assert info["stop_reason"] == "submitted"  # 4 < 5

    # 轨迹里的提交结果也要带上层数，复盘时一眼能看到
    from fivewhys.trace import read_events

    submitted = [
        event
        for event in read_events(writer.path)
        if event["kind"] == "tool_result" and event["tool"] == "submit_diagnosis"
    ]
    assert "追问 4 层" in submitted[0]["result"]
    assert run.stop_reason == "submitted"


def test_evidence_check_is_independent_of_the_depth_label() -> None:
    """两个校验各管各的：链短但证据编造 → 仍然被拒。"""
    payload = json.loads(diagnosis_payload())
    diagnosis = Diagnosis.model_validate(
        {
            **payload,
            "why_chain": _why_chain(1),
            "evidence": [{"source": "query_metrics", "finding": "x"}],
        }
    )

    problems = check_evidence(
        diagnosis,
        [ToolCallRecord(step=1, tool="query_logs", args={}, ok=True)],
        known_tools=["query_logs", "query_metrics"],
    )
    assert problems, "证据编造与追问层数无关，该拒还是要拒"
