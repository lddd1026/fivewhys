"""`LiteLLMClient` 的解析测试 —— 补上「最后一公里」的覆盖。

## 为什么需要这个文件

之前所有测试都用 ``ScriptedLLM``（我们的测试替身），
**真正接触 litellm 的那段代码从来没被执行过**：

    LLMResponse(content=..., tool_calls=..., prompt_tokens=...)

如果 ``LiteLLMClient.complete()`` 里对 litellm 响应对象的解析写错了，
前面 53 个测试会全部通过，而 FIV-5 一跑就挂 —— 而且报的错会指向别处。

这里用**假的 litellm 响应对象**喂进去，把真实代码路径跑一遍，
不开网络、不花钱、结果确定。

对应需求：FR-6、FR-9（轨迹里的 token / 成本统计）
"""

from __future__ import annotations

from typing import Any

import pytest

from fivewhys.agent.llm import LiteLLMClient

# --------------------------------------------------------------------------
# 伪造 litellm 的响应对象
#
# 结构照着 litellm 的真实返回造：
#   response.choices[0].message.content
#   response.choices[0].message.tool_calls[i].function.{name,arguments}
#   response.usage.{prompt_tokens,completion_tokens}
# --------------------------------------------------------------------------


class FakeFunction:
    def __init__(self, name: str | None, arguments: str | None) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str | None, name: str | None, arguments: str | None) -> None:
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str | None, tool_calls: list[FakeToolCall] | None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


class FakeResponse:
    def __init__(
        self,
        message: FakeMessage,
        usage: FakeUsage | None = None,
    ) -> None:
        self.choices = [FakeChoice(message)]
        self.usage = usage


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """构造一个 LiteLLMClient，并把它的传输层换成可控的假实现。"""
    import litellm

    captured: dict[str, Any] = {}

    def make(responder: Any) -> Any:
        async def fake_acompletion(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return responder(kwargs)

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        instance = LiteLLMClient(model="deepseek/deepseek-chat")
        instance.captured = captured
        return instance

    return make


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


async def test_parses_text_response(client: Any) -> None:
    instance = client(lambda _: FakeResponse(FakeMessage("我先看看日志。", None), FakeUsage(10, 5)))
    result = await instance.complete([{"role": "user", "content": "hi"}], [])

    assert result.content == "我先看看日志。"
    assert result.tool_calls == ()
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5
    assert result.total_tokens == 15


async def test_parses_tool_calls(client: Any) -> None:
    payload = '{"service": "order-service"}'
    response = FakeResponse(
        FakeMessage(None, [FakeToolCall("call_abc", "query_logs", payload)]),
        FakeUsage(100, 20),
    )
    instance = client(lambda _: response)
    result = await instance.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])

    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "query_logs"
    # arguments 必须是**原始字符串**，不能在这里解析 —— 见 llm.py 的模块 docstring
    assert call.arguments == payload


async def test_missing_usage_defaults_to_zero(client: Any) -> None:
    """不是所有 provider 都返回 usage；缺了不能让整个诊断崩掉。"""
    instance = client(lambda _: FakeResponse(FakeMessage("ok", None), usage=None))
    result = await instance.complete([], [])

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.total_tokens == 0


async def test_none_arguments_become_empty_object(client: Any) -> None:
    """有些 provider 无参数时返回 arguments=None，而不是 "{}"。"""
    response = FakeResponse(FakeMessage(None, [FakeToolCall("c1", "some_tool", None)]))
    instance = client(lambda _: response)
    result = await instance.complete([], [])

    assert result.tool_calls[0].arguments == "{}"


async def test_missing_tool_call_id_is_filled_in(client: Any) -> None:
    """id 为空时补一个 —— 否则回填 tool 消息时会丢失关联。"""
    response = FakeResponse(FakeMessage(None, [FakeToolCall(None, "some_tool", "{}")]))
    instance = client(lambda _: response)
    result = await instance.complete([], [])

    assert result.tool_calls[0].id == "call_0"


async def test_cost_estimation_failure_is_swallowed(client: Any) -> None:
    """成本估算失败不能影响诊断 —— 新模型可能不在价格表里。"""
    import litellm

    def boom(**_kwargs: Any) -> float:
        raise ValueError("模型不在价格表里")

    instance = client(lambda _: FakeResponse(FakeMessage("ok", None)))
    original = litellm.completion_cost
    litellm.completion_cost = boom  # type: ignore[assignment]
    try:
        result = await instance.complete([], [])
    finally:
        litellm.completion_cost = original  # type: ignore[assignment]

    assert result.cost_usd == 0.0


# --------------------------------------------------------------------------
# 请求参数
# --------------------------------------------------------------------------


async def test_empty_tools_are_not_sent(client: Any) -> None:
    """tools=[] 会让部分 provider 报错，干脆不传这个键。"""
    instance = client(lambda _: FakeResponse(FakeMessage("ok", None)))
    await instance.complete([{"role": "user", "content": "hi"}], [])

    assert "tools" not in instance.captured
    assert "tool_choice" not in instance.captured


async def test_tools_are_sent_with_auto_choice(client: Any) -> None:
    instance = client(lambda _: FakeResponse(FakeMessage("ok", None)))
    specs = [{"type": "function", "function": {"name": "t"}}]
    await instance.complete([], specs)

    assert instance.captured["tools"] == specs
    assert instance.captured["tool_choice"] == "auto"
    assert instance.captured["temperature"] == 0.0


async def test_api_base_is_forwarded_when_set(client: Any) -> None:
    """自定义端点要真的传下去 —— 否则请求会打到官方接口上。

    这是本地假 LLM 服务能工作的前提（端到端测试就靠它）。
    """
    instance = client(lambda _: FakeResponse(FakeMessage("ok", None)))
    instance.api_base = "http://127.0.0.1:9"
    await instance.complete([], [])

    assert instance.captured["api_base"] == "http://127.0.0.1:9"


async def test_api_base_is_omitted_when_none(client: Any) -> None:
    """没设置时不能传 api_base=None —— 部分 provider 会因此报错。"""
    instance = client(lambda _: FakeResponse(FakeMessage("ok", None)))
    await instance.complete([], [])

    assert "api_base" not in instance.captured


def test_local_cost_map_is_enabled(client: Any) -> None:
    """离线价格表开关必须在 import litellm 之前设上。

    否则 litellm 会去 GitHub 拉价格表，网络不通时重试 3 次、耗时约 30 秒，
    直接爆掉 NFR-3 的延迟预算（而且它不报错，只是慢）。
    """
    import os

    client(lambda _: FakeResponse(FakeMessage("ok", None)))
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


# --------------------------------------------------------------------------
# 与主循环串起来的集成测试
#
# 这是「不开网络的最接近 FIV-5 的测试」：
# LiteLLMClient 是真的，litellm.acompletion 是假的，
# 整条路径（请求构造 → 响应解析 → 工具分发 → 终止）全部走一遍。
# --------------------------------------------------------------------------


async def test_full_loop_with_real_client_and_faked_transport(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import Counter
    from datetime import UTC, datetime, timedelta

    from doubles import diagnosis_payload
    from fivewhys.agent import SUBMIT_TOOL_NAME, diagnose
    from fivewhys.config import Settings
    from fivewhys.mock.logstore import LogStore
    from fivewhys.mock.scenarios import inject_db_pool_exhausted
    from fivewhys.mock.service import MockService
    from fivewhys.tools import build_registry

    t0 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    fault_at = t0 + timedelta(minutes=5)
    store = LogStore()
    service = MockService("order-service", store)
    service.normal_operation(t0, fault_at)
    truth = inject_db_pool_exhausted(store, service, fault_at)

    rounds = Counter()

    def responder(_kwargs: dict[str, Any]) -> FakeResponse:
        rounds["n"] += 1
        if rounds["n"] == 1:
            args = (
                '{"service": "order-service", '
                f'"start": "{fault_at.isoformat()}", '
                f'"end": "{(fault_at + timedelta(minutes=5)).isoformat()}"}}'
            )
            return FakeResponse(
                FakeMessage(None, [FakeToolCall("c1", "query_logs", args)]), FakeUsage(500, 40)
            )
        return FakeResponse(
            FakeMessage(
                None,
                [FakeToolCall("c2", SUBMIT_TOOL_NAME, diagnosis_payload())],
            ),
            FakeUsage(800, 120),
        )

    llm = client(responder)
    run = await diagnose(
        scenario_id=truth.scenario_id,
        question="order-service 从 14:05 前后开始错误率飙升，帮忙定位一下原因",
        registry=build_registry(store),
        llm=llm,
        settings=Settings(_env_file=None, max_steps=5, max_cost_usd=1.0),  # type: ignore[call-arg]
    )

    assert run.stop_reason == "submitted"
    assert run.diagnosis is not None
    assert run.diagnosis.root_cause_service == truth.root_cause_service
    assert run.diagnosis.fault_category == truth.fault_category

    # 工具调用被真实执行了，而且结果非空
    assert len(run.tool_calls) == 1
    assert run.tool_calls[0].ok is True
    assert "共命中" in run.tool_calls[0].result_summary

    assert run.total_tokens == 500 + 40 + 800 + 120
    assert run.steps == 2
