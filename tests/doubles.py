"""测试替身：一个「按脚本返回响应」的假 LLM 客户端。

## 为什么需要它

agent 主循环的逻辑（工具分发、错误处理、终止条件、成本熔断）**和模型无关**。
有了这个替身，这些逻辑可以**脱离网络和 API Key** 做确定性测试。

否则每跑一次单测就要联网、要花钱、结果还不稳定 —— 那就不叫单测了。

## 用法

    llm = ScriptedLLM([tool_call("query_logs", {...}), submit()])
    run = await diagnose(scenario_id="s1", question="...", registry=reg, llm=llm)

脚本里的每一项是**一轮**模型的响应。脚本用完还没结束，说明循环没按预期停止，
``complete()`` 会直接报错 —— 这本身就是一种断言。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from fivewhys.agent.llm import LLMResponse, ToolCall

# --------------------------------------------------------------------------
# 构造响应
# --------------------------------------------------------------------------


def call(
    name: str,
    arguments: dict[str, Any] | str | None = None,
    *,
    call_id: str = "call_1",
    cost_usd: float = 0.0,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> LLMResponse:
    """构造一轮「模型要求调用工具」的响应。"""
    if arguments is None:
        raw = "{}"
    elif isinstance(arguments, str):
        raw = arguments  # 故意支持传坏字符串，测试 JSON 解析失败的分支
    else:
        raw = json.dumps(arguments, ensure_ascii=False)
    return LLMResponse(
        tool_calls=(ToolCall(id=call_id, name=name, arguments=raw),),
        cost_usd=cost_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def say(
    content: str = "让我先看看日志。",
    *,
    cost_usd: float = 0.0,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
) -> LLMResponse:
    """构造一轮「模型只说话，不调工具」的响应。"""
    return LLMResponse(
        content=content,
        cost_usd=cost_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def diagnosis_payload(**overrides: Any) -> str:
    """构造一份合法的 ``Diagnosis`` JSON 字符串。"""
    payload: dict[str, Any] = {
        "root_cause": "order-service 的数据库连接池上限被配置变更下调，导致连接耗尽",
        "root_cause_service": "order-service",
        "fault_category": "db_pool_exhausted",
        "confidence": "high",
        "why_chain": [
            {
                "depth": 1,
                "question": "为什么请求失败？",
                "answer": "等数据库连接超时",
                "evidence": [],
            }
        ],
        "evidence": [
            {
                "source": "query_logs(order-service)",
                "finding": "connection wait time 从 0ms 飙升到 3000ms",
                "supports": True,
            }
        ],
        "ruled_out": ["下游服务在该窗口内无异常日志"],
        "suggested_fix": "回滚配置变更",
        "summary": "连接池耗尽导致大面积超时",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def submit(
    *,
    cost_usd: float = 0.0,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    **overrides: Any,
) -> LLMResponse:
    """构造一轮「模型提交结论」的响应。``overrides`` 用来改结论内容。"""
    return call(
        "submit_diagnosis",
        json.loads(diagnosis_payload(**overrides)),
        call_id="submit_1",
        cost_usd=cost_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# --------------------------------------------------------------------------
# 假客户端
# --------------------------------------------------------------------------


class ScriptedLLM:
    """按脚本逐轮返回预设响应的假 LLM 客户端。

    ``script`` 里的每一项要么是 :class:`LLMResponse`，
    要么是一个异常实例（用来模拟网络故障等）。
    """

    def __init__(
        self,
        script: Sequence[LLMResponse | Exception],
        *,
        model: str = "scripted/fake",
    ) -> None:
        self._script = list(script)
        self._index = 0
        self.model = model
        # 记录每次请求，方便断言「喂回去的内容对不对」
        self.requests: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> LLMResponse:
        self.requests.append({"messages": list(messages), "tools": list(tools)})
        if self._index >= len(self._script):
            raise AssertionError(f"脚本在第 {self._index + 1} 轮用完了 —— 说明循环没有按预期停止。")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item

    # ---- 断言辅助 ----

    @property
    def rounds(self) -> int:
        """实际发起了几轮请求。"""
        return len(self.requests)

    def last_messages(self) -> list[dict[str, Any]]:
        """最后一轮请求发给模型的消息列表。"""
        return self.requests[-1]["messages"]

    def all_content(self) -> str:
        """把所有消息拼成一个字符串，方便做「喂回去了吗」这类断言。"""
        chunks: list[str] = []
        for request in self.requests:
            for message in request["messages"]:
                chunks.append(str(message.get("content", "")))
                for tool_call in message.get("tool_calls", []) or []:
                    chunks.append(json.dumps(tool_call, ensure_ascii=False))
        return "\n".join(chunks)


__all__ = ["ScriptedLLM", "call", "diagnosis_payload", "say", "submit"]
