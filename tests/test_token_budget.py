"""token 用量控制（需求 NFR-12 / FR-7 的 `max_tokens`）。

## 为什么要单独一套 token 上限

项目原本只有**成本**上限，而成本这条路依赖 litellm 的价格表 —— FIV-D3 那个 bug
就是价格表查不到模型 → `completion_cost` 抛异常被吞 → 成本恒为 0 →
**$0.10 的硬上限形同虚设**。

token 不一样：它是 provider 在 `usage` 里直接报的，不依赖任何价格表。
所以 token 上限是**兜底中的兜底**。

另外还有一件成本管不到的事：**上下文会自己长大**。每步都把工具返回追加进历史，
20 步下来 prompt 可能涨到几十万 token —— 那是「上下文溢出」，
provider 会直接报错，而那时钱已经花了。所以有两道闸门：

=======================  ==========================  ==========================
闸门                     在哪拦                       用哪个数字
=======================  ==========================  ==========================
单次请求上下文上限        发出去**之前**               按字符估（3 字符 ≈ 1 token）
单次诊断累计 token 上限    每步收到 usage **之后**      provider 上报的硬数字
=======================  ==========================  ==========================
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doubles import ScriptedLLM, call, submit  # type: ignore[import-not-found]
from fivewhys.agent import diagnose
from fivewhys.agent.llm import CHARS_PER_TOKEN, estimate_request_tokens, estimate_tokens
from fivewhys.config import Settings
from fivewhys.trace import read_events


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "max_steps": 10,
        "max_cost_usd": 1.0,
        "trace_enabled": False,
        # ⚠️ 关掉证据校验：这个文件测的是别的机制（token 预算 / 轨迹），
        # 脚本里的结论没有引用任何真实工具调用 —— 开了校验会被拒（那是 FIV-17 的正确行为）。
        # 证据校验本身在 tests/test_evidence.py 里用**默认值**测。
        "verify_evidence": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _registry():
    from fivewhys.mock.logstore import LogStore
    from fivewhys.tools import DataSource, build_registry

    return build_registry(DataSource.logs_only(LogStore()))


def _query_call(*, tokens: int):
    return call(
        "query_logs",
        {"service": "order-service"},
        prompt_tokens=tokens,
        completion_tokens=0,
    )


# --------------------------------------------------------------------------
# 估算器本身
# --------------------------------------------------------------------------


def test_estimate_tokens_uses_the_documented_ratio() -> None:
    assert estimate_tokens("x" * 300) == 300 // CHARS_PER_TOKEN
    assert estimate_tokens("") == 1, "空文本也要给个下限，别算出 0"


def test_estimate_request_tokens_counts_content_and_tool_specs() -> None:
    messages = [{"role": "user", "content": "x" * 300}]
    without_tools = estimate_request_tokens(messages, [])
    assert without_tools == 100

    with_tools = estimate_request_tokens(
        messages, [{"type": "function", "function": {"name": "t"}}]
    )
    assert with_tools > without_tools, "工具说明也是要发出去的内容，不能不算"


def test_estimate_request_tokens_counts_tool_call_arguments() -> None:
    """历史里那些 tool_calls 的 arguments 也要算 —— 它们同样占用上下文。"""
    plain = estimate_request_tokens([{"role": "assistant", "content": ""}], [])
    with_args = estimate_request_tokens(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"arguments": "y" * 300}}],
            }
        ],
        [],
    )
    assert with_args > plain


# --------------------------------------------------------------------------
# ⭐ 累计 token 上限
# --------------------------------------------------------------------------


async def test_total_token_budget_stops_the_loop() -> None:
    """⭐ 累计 token 超过上限就停，并说明**是谁**触发的。"""
    llm = ScriptedLLM([_query_call(tokens=600) for _ in range(5)])

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_total_tokens=1_000),
    )

    assert run.stop_reason == "max_tokens"
    assert run.stop_note is not None
    assert "1,000" in run.stop_note and "token" in run.stop_note
    assert run.total_tokens > 1_000, "触发条件就是「超过」"
    assert llm.rounds <= 2, "第 2 步之后就该停，不该继续烧"


async def test_token_budget_does_not_steal_a_valid_submission() -> None:
    """⭐ 已经交出合法结论的那一步，不该因为「刚好多花几个 token」被丢掉。

    顺序很关键：token 检查必须放在「是否已提交结论」**之后**。
    """
    llm = ScriptedLLM([submit(prompt_tokens=5_000)])

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_total_tokens=1_000),
    )

    assert run.stop_reason == "submitted", "有合法结论时不该被 token 上限盖掉"
    assert run.diagnosis is not None
    assert run.total_tokens > 1_000


async def test_cost_and_token_budgets_are_independent() -> None:
    """token 有上限但成本没超（或反过来）时，各自按自己的规则停。

    这条守的是「两个上限不能互相顶掉」—— token 是兜底，成本是精细化控制。
    """
    # token 很多但单价为 0 → 只有 token 闸门会响
    llm = ScriptedLLM([_query_call(tokens=2_000) for _ in range(3)])

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_total_tokens=1_000, max_cost_usd=100.0),
    )

    assert run.stop_reason == "max_tokens"
    assert run.total_cost_usd == 0.0


# --------------------------------------------------------------------------
# ⭐ 上下文守卫（发出去之前）
# --------------------------------------------------------------------------


async def test_context_guard_stops_before_sending_an_oversized_request() -> None:
    """⭐ 上下文涨满时，**在发出去之前**就停，而不是等 provider 报错。

    等 provider 报错的话：这一轮的钱已经花了，而且用户看到的是一个
    provider 异常（看不懂），不是一个「上下文满了」的明确说明。
    """
    # 每次工具返回都很长 → 历史迅速涨大（工具返回会原样进下一轮的 prompt）
    llm = ScriptedLLM(
        [
            call("query_logs", {"service": "order-service", "keyword": "x" * 5_000}),
            submit(),
        ]
    )

    run = await diagnose(
        scenario_id="s1",
        question="q" * 1_000,
        registry=_registry(),
        llm=llm,
        settings=_settings(max_context_tokens=500, max_total_tokens=10_000_000),
    )

    assert run.stop_reason == "max_tokens"
    assert run.stop_note is not None
    assert "上下文上限" in run.stop_note
    assert "预估" in run.stop_note, "要说清这是**预估**，不是 provider 报的数字"


async def test_context_guard_does_not_trigger_on_a_normal_run() -> None:
    """别把闸门设得太紧 —— 正常的一次诊断必须能跑完。"""
    llm = ScriptedLLM([call("query_logs", {"service": "order-service"}), submit()])

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_context_tokens=60_000),
    )

    assert run.stop_reason == "submitted"


# --------------------------------------------------------------------------
# 停止说明要能被看见（轨迹 + 报表）
# --------------------------------------------------------------------------


async def test_stop_note_is_written_to_the_trace(tmp_path: Path) -> None:
    """停止说明必须进轨迹 —— 复盘时第一个要看的就是「为什么停了」。"""
    from fivewhys.trace import TraceWriter

    llm = ScriptedLLM([_query_call(tokens=600) for _ in range(5)])
    writer = TraceWriter("token-run", root=tmp_path)

    run = await diagnose(
        scenario_id="s1",
        question="q",
        registry=_registry(),
        llm=llm,
        settings=_settings(max_total_tokens=1_000),
        trace=writer,
    )

    finish = next(e for e in read_events(writer.path) if e["kind"] == "finish")
    assert finish["stop_reason"] == "max_tokens"
    assert finish["stop_note"] == run.stop_note
    assert "1,000" in finish["stop_note"]


def test_defaults_are_generous_enough_for_a_real_run() -> None:
    """默认值不能误伤真实运行：实测一次 5 步诊断约 48k token、上下文约 20k。

    FIV-16 之后 ``max_context_tokens`` 默认是 ``None`` = **按模型的窗口自动算**。
    所以这里断言的是**最终生效的那个闸门**，而不是配置字段本身 ——
    「默认值够不够大」这件事，只有算完之后才回答得了。
    """
    from fivewhys.providers import UNKNOWN_MODEL_CONTEXT_TOKENS, describe_model

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.max_total_tokens >= 100_000
    assert settings.max_context_tokens is None, "默认按模型窗口自动算，不写死"

    gate = describe_model(settings.llm_model).context_limit(override=settings.max_context_tokens)
    assert gate >= UNKNOWN_MODEL_CONTEXT_TOKENS, f"闸门 {gate} 太小，会误伤正常诊断"


def test_token_limits_are_configurable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """设了具体数字就按它来 —— 这是复现实验时唯一能固定闸门的办法。"""
    monkeypatch.setenv("FIVEWHYS_MAX_TOTAL_TOKENS", "123456")
    monkeypatch.setenv("FIVEWHYS_MAX_CONTEXT_TOKENS", "45678")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.max_total_tokens == 123_456
    assert settings.max_context_tokens == 45_678
    # 显式配置优先于「按窗口自动算」
    from fivewhys.providers import describe_model

    assert (
        describe_model(settings.llm_model).context_limit(override=settings.max_context_tokens)
        == 45_678
    )
