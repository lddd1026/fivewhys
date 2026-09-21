"""FIV-19 验收测试：轨迹落盘（需求 FR-9）。

## 这个模块为什么存在

上线前审查里有一条至今没查明的失败：

    第一版 M1 验收是「100%（5/5）」，后来又跑了几批，
    其中一批 2 次里有 1 次没到 80 分 —— **那次的轨迹没留下来，
    所以不知道它错在哪。**

所以下面每条断言，本质都在回答同一个问题：
**下一次失败的时候，我能不能查出来它错在哪？**

- 每一步的 prompt / 响应 / token / 成本 / 耗时 → 能复盘模型看到了什么
- 工具调用与返回**原样**存 → 能确认「模型是不是被工具输出带偏了」
- 异常路径也有 finish → 能区分「跑完了」和「进程被杀」
- 崩溃后前几步仍在盘上 → 半截轨迹也是线索
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doubles import call, submit  # type: ignore[import-not-found]
from fivewhys.agent import diagnose
from fivewhys.config import Settings
from fivewhys.trace import (
    EVENT_FINISH,
    EVENT_REQUEST,
    EVENT_RESPONSE,
    EVENT_START,
    EVENT_TOOL_RESULT,
    TraceWriter,
    find_run,
    new_run_id,
    read_events,
    summarize,
)

T0_QUESTION = "order-service 从 14:02 前后开始错误率飙升，帮忙定位一下原因"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "max_steps": 5,
        "max_cost_usd": 1.0,
        "trace_enabled": False,  # 由测试自己传 writer，避免写到仓库里
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _registry():
    from fivewhys.mock.logstore import LogStore
    from fivewhys.tools import DataSource, build_registry

    return build_registry(DataSource.logs_only(LogStore()))


async def _run(tmp_path: Path, llm, **settings_overrides: object):
    """跑一次诊断，轨迹写到 tmp_path。"""
    writer = TraceWriter("test-run-0001", root=tmp_path)
    run = await diagnose(
        scenario_id="s1",
        question=T0_QUESTION,
        registry=_registry(),
        llm=llm,
        settings=_settings(**settings_overrides),
        trace=writer,
    )
    return run, writer


# --------------------------------------------------------------------------
# ⭐ 一次成功的诊断，轨迹里该有什么
# --------------------------------------------------------------------------


async def test_trace_records_the_whole_investigation(tmp_path: Path) -> None:
    """⭐ 从「模型看到了什么」到「它最后交了什么」，一步都不能少。"""
    from doubles import ScriptedLLM

    run, writer = await _run(
        tmp_path,
        ScriptedLLM([call("query_logs", {"service": "order-service"}), submit()]),
    )

    assert writer.path.is_file(), "轨迹文件没生成"
    events = read_events(writer.path)
    kinds = [event["kind"] for event in events]

    assert kinds[0] == EVENT_START
    assert kinds[-1] == EVENT_FINISH
    assert EVENT_REQUEST in kinds, "没记下每一步发给模型的 prompt"
    assert EVENT_RESPONSE in kinds, "没记下模型的响应"
    assert EVENT_TOOL_RESULT in kinds, "没记下工具调用结果"

    # 每一步都必须配一对 request / response —— 缺一边就没法复盘
    steps_with_response = {e["step"] for e in events if e["kind"] == EVENT_RESPONSE}
    steps_with_request = {e["step"] for e in events if e["kind"] == EVENT_REQUEST}
    assert steps_with_response == steps_with_request

    assert run.stop_reason == "submitted"


async def test_start_event_has_everything_needed_to_reproduce(tmp_path: Path) -> None:
    """start 事件要说清「这次是怎么跑的」—— 约束 C-7：数字必须能对应到配置。"""
    from doubles import ScriptedLLM

    _, writer = await _run(tmp_path, ScriptedLLM([submit()]))
    start = read_events(writer.path)[0]

    assert start["scenario_id"] == "s1"
    assert start["question"] == T0_QUESTION
    assert start["model"]
    assert "query_logs" in start["tools"]
    # 配置也记下来，否则「这组数字是用什么参数跑出来的」说不清
    assert start["settings"]["max_steps"] == 5
    assert "temperature" in start["settings"]


async def test_request_event_contains_the_system_prompt_and_question(tmp_path: Path) -> None:
    """prompt 要**完整**存下来 —— 复盘的第一步就是看模型当时看到了什么。"""
    from doubles import ScriptedLLM

    _, writer = await _run(tmp_path, ScriptedLLM([submit()]))
    request = next(e for e in read_events(writer.path) if e["kind"] == EVENT_REQUEST)

    roles = [message["role"] for message in request["messages"]]
    assert roles[0] == "system"
    assert "user" in roles
    system = request["messages"][0]["content"]
    assert "fivewhys" in system, "系统提示词没被记下来"
    assert T0_QUESTION in "".join(str(m.get("content", "")) for m in request["messages"])


async def test_response_event_records_tokens_cost_and_latency(tmp_path: Path) -> None:
    """token / 成本 / 耗时 —— M6 的指标全靠这几项。"""
    from doubles import ScriptedLLM

    _, writer = await _run(tmp_path, ScriptedLLM([submit()]))
    response = next(e for e in read_events(writer.path) if e["kind"] == EVENT_RESPONSE)

    for key in (
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "latency_ms",
        "content",
        "tool_calls",
    ):
        assert key in response, f"response 事件缺 {key}"
    assert response["prompt_tokens"] > 0
    assert "latency_ms" in response


async def test_tool_result_is_stored_verbatim(tmp_path: Path) -> None:
    """工具返回**原样**存，不截断。

    工具返回是 agent 唯一的信息来源。截断它 = 改掉「模型看到了什么」，
    复盘就会得出错误结论。
    """
    from doubles import ScriptedLLM

    _, writer = await _run(
        tmp_path,
        ScriptedLLM(
            [
                call(
                    "query_logs",
                    {
                        "service": "order-service",
                        "start": "2026-01-01T14:00:00Z",
                        "end": "2026-01-01T14:05:00Z",
                    },
                ),
                submit(),
            ]
        ),
    )
    tool_event = next(e for e in read_events(writer.path) if e["kind"] == EVENT_TOOL_RESULT)

    assert tool_event["tool"] == "query_logs"
    assert tool_event["ok"] is True
    assert tool_event["args"]["service"] == "order-service"
    assert "共命中" in tool_event["result"] or "线索" in tool_event["result"]


async def test_failed_tool_call_is_recorded_too(tmp_path: Path) -> None:
    """失败的调用同样要记 —— 「它试了什么、错在哪」往往比成功的更有价值。"""
    from doubles import ScriptedLLM

    _, writer = await _run(
        tmp_path,
        ScriptedLLM([call("query_traces", {"nope": 1}), submit()]),
    )
    tool_event = next(e for e in read_events(writer.path) if e["kind"] == EVENT_TOOL_RESULT)

    assert tool_event["ok"] is False
    assert "未知工具" in (tool_event["error"] or "")


async def test_finish_event_matches_the_run(tmp_path: Path) -> None:
    """finish 里的总量必须和 AgentRun 对得上 —— 两处数字不一致就等于都没有。"""
    from doubles import ScriptedLLM

    run, writer = await _run(tmp_path, ScriptedLLM([submit()]))
    finish = read_events(writer.path)[-1]

    assert finish["stop_reason"] == run.stop_reason == "submitted"
    assert finish["total_cost_usd"] == pytest.approx(run.total_cost_usd)
    assert finish["total_tokens"] == run.total_tokens
    assert finish["diagnosis"] is not None
    assert finish["diagnosis"]["root_cause_service"]


async def test_every_line_is_valid_json(tmp_path: Path) -> None:
    """JSONL 的每一行都要能被解析 —— 否则下游（M6/M7）读不动。"""
    from doubles import ScriptedLLM

    _, writer = await _run(tmp_path, ScriptedLLM([call("query_logs", {}), submit()]))

    lines = [line for line in writer.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 4
    for index, line in enumerate(lines, start=1):
        parsed = json.loads(line)  # 解析失败会直接抛，这就是断言
        assert "kind" in parsed, f"第 {index} 行没有 kind"
        assert "ts" in parsed, f"第 {index} 行没有时间戳"


# --------------------------------------------------------------------------
# ⭐ 失败的时候，轨迹还在不在
# --------------------------------------------------------------------------


async def test_trace_survives_a_crash_mid_run(tmp_path: Path) -> None:
    """⭐ 跑到一半崩了，**前面的步骤必须还在盘上**。

    这条是 JSONL「边跑边写」的全部意义。写成一个 JSON 的话，
    中途崩溃等于什么都没留下 —— 而那正是最需要轨迹的时候。
    """
    from doubles import ScriptedLLM

    # 第 1 步正常查日志；第 2 步 LLM 调用直接抛（模拟 provider 挂了 / 网络断）
    llm = ScriptedLLM(
        [
            call("query_logs", {"service": "order-service"}),
            RuntimeError("provider 挂了"),
        ]
    )

    run, writer = await _run(tmp_path, llm)

    assert run.stop_reason == "error"
    events = read_events(writer.path)
    assert any(e["kind"] == EVENT_REQUEST for e in events), "崩之前的 prompt 丢了"
    assert any(e["kind"] == EVENT_TOOL_RESULT for e in events), "崩之前的工具结果丢了"
    assert events[-1]["kind"] == EVENT_FINISH, "异常路径没有收尾事件"
    assert "provider 挂了" in (events[-1]["error"] or "")


async def test_broken_lines_do_not_break_reading(tmp_path: Path) -> None:
    """最后一行写了一半也不能让整个轨迹读不出来 —— 那本身就是线索。"""
    writer = TraceWriter("half-written", root=tmp_path)
    writer._append("response", {"step": 1, "content": "ok"})
    with writer.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "response", "step": 2, "cont')  # 故意截断

    events = read_events(writer.path)

    assert len(events) == 2
    assert events[0]["content"] == "ok"
    assert events[1]["kind"] == "broken", "坏行应该被标出来，而不是让整个读取失败"


# --------------------------------------------------------------------------
# 命名、查找、摘要
# --------------------------------------------------------------------------


def test_run_id_is_sortable_and_unique() -> None:
    """可排序（按时间）+ 唯一（并发跑评测不会互相覆盖）。"""
    from datetime import UTC, datetime

    at = datetime(2026, 1, 1, 14, 2, 0, tzinfo=UTC)
    first = new_run_id("order-service-db-pool", at=at)
    second = new_run_id("order-service-db-pool", at=at)

    assert first.startswith("order-service-db-pool-20260101T140200-")
    assert first != second, "同一秒内两次运行不能撞名 —— 撞名会覆盖掉失败的那次"


def test_find_run_supports_prefix_and_latest(tmp_path: Path) -> None:
    for run_id in (
        "order-service-db-pool-20260101T140200-aaaa",
        "payment-service-x-20260101T150000-bbbb",
    ):
        writer = TraceWriter(run_id, root=tmp_path)
        writer._append(EVENT_START, {"run_id": run_id})

    assert find_run("order-service", root=tmp_path).parent.name.endswith("aaaa")
    assert find_run("order-service-db-pool-20260101T140200-aaaa", root=tmp_path)
    assert find_run("latest", root=tmp_path).is_file()


def test_find_run_reports_ambiguity_instead_of_guessing(tmp_path: Path) -> None:
    """前缀匹配到多个时要说清楚，不能随便挑一个 —— 挑错就复盘错。"""
    for suffix in ("aaaa", "bbbb"):
        TraceWriter(f"order-service-db-pool-20260101T140200-{suffix}", root=tmp_path)._append(
            EVENT_START, {}
        )

    with pytest.raises(FileNotFoundError, match="匹配到 2 个"):
        find_run("order-service-db-pool", root=tmp_path)


def test_find_run_says_what_to_do_when_empty(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="先跑一次诊断"):
        find_run("latest", root=tmp_path)


def test_summarize_gives_a_one_glance_view(tmp_path: Path) -> None:
    """CLI 用的一眼摘要。刻意只统计事实，不做解释 —— 工具歪曲事实比不给工具更糟。"""
    writer = TraceWriter("s1-20260101T140200-aaaa", root=tmp_path)
    writer.start(
        scenario_id="s1",
        question="q",
        model="m",
        tool_names=["query_logs"],
        settings={},
    )
    writer.response(
        step=1,
        content=None,
        tool_calls=[],
        prompt_tokens=100,
        completion_tokens=20,
        cost_usd=0.001,
        latency_ms=1200,
    )
    writer.tool_result(step=1, tool="query_logs", args={}, ok=False, result="", error="boom")
    writer.finish(_run_stub())

    info = summarize(writer.path)

    assert info["scenario_id"] == "s1"
    assert info["steps"] == 1
    assert info["tool_calls"] == 1
    assert info["failed_tool_calls"] == 1
    assert info["has_finish"] is True
    assert info["broken_lines"] == 0


def _run_stub():
    from fivewhys.models import AgentRun

    return AgentRun(
        scenario_id="s1",
        model="m",
        stop_reason="max_steps",
        total_cost_usd=0.001,
        total_tokens=120,
    )


# --------------------------------------------------------------------------
# 默认行为：需求说「必须落盘」
# --------------------------------------------------------------------------


async def test_tracing_is_on_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 FR-9 说的是**每次**诊断都必须落盘 —— 默认关掉等于出事的证据永远没有。"""
    from doubles import ScriptedLLM

    monkeypatch.chdir(tmp_path)  # 默认根目录是相对的 runs/
    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=ScriptedLLM([submit()]),
        settings=_settings(trace_enabled=True, trace_root=Path("runs")),
    )

    assert run.trace_path is not None
    assert Path(run.trace_path).is_file()
    assert Path(run.trace_path).parent.parent.name == "runs"
    assert run.run_id


async def test_trace_is_not_written_when_disabled(tmp_path: Path) -> None:
    """测试和「不想留痕」的场景要能关掉。"""
    from doubles import ScriptedLLM

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=ScriptedLLM([submit()]),
        settings=_settings(trace_enabled=False),
    )

    assert run.trace_path is None
    assert run.run_id == ""
    assert not list(tmp_path.glob("runs/*"))


async def test_the_whole_trace_never_contains_a_secret(tmp_path: Path) -> None:
    """轨迹里不许出现密钥 —— 它会被共享、被贴进 issue、被 M6 读。

    （轨迹本身不进版本库，但它比源码更容易被随手发出去。）
    """
    import os

    from doubles import ScriptedLLM

    os.environ["DEEPSEEK_API_KEY"] = "sk-CANARYtrace11223344556677889900"
    try:
        _, writer = await _run(tmp_path, ScriptedLLM([submit()]))
        text = writer.path.read_text(encoding="utf-8")
        assert "CANARY" not in text, "轨迹把环境里的密钥写进去了"
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
