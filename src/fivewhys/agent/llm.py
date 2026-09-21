"""LLM 调用层 —— 把「调用模型」这件事抽出来，便于替换。

## 为什么要单独一层

agent 主循环的逻辑（工具分发、终止条件、错误处理、成本累加）**和模型无关**。
如果没有这层，测试循环逻辑就必须真的调用模型 —— 也就是每次都要网络和 API Key。

抽出这层之后，测试可以注入一个「按脚本返回响应」的假客户端，
循环逻辑就能脱离网络做**确定性**测试。这是 FIV-4c 的基础。

## 为什么 ``arguments`` 故意保持未解析

``ToolCall.arguments`` 是模型返回的**原始 JSON 字符串**，这里不解析。

因为「模型返回非法 JSON」是这个项目要展示的真实失败模式之一（M7 要优化它）。
如果在这一层悄悄解析掉，循环里就看不到这个失败，也就无从统计和改进。

**解析和容错是主循环的责任，不是这一层的。**
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ⚠️ 必须在【任何地方 import litellm 之前】执行，所以放在模块级而不是构造函数里。
#
# litellm 一被 import 就会去 GitHub 拉最新的模型价格表；网络不通时它重试 3 次
# 才回退到本地备份 —— 实测这一下要 60 秒以上，直接爆掉 NFR-3 的延迟预算。
# 因为它不报错、只是慢，很容易被忽略。
#
# 曾经把这行放在 LiteLLMClient.__init__ 里，结果被 import 顺序打败了：
# 只要有人先 `import litellm`，开关就来不及生效，测试套件从 2 秒变成 68 秒。
# 这个坑是跑 --durations 时发现的。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# OpenAI 风格的消息。litellm 会把各家 provider 的差异抹平到这种格式。
Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """模型要求调用的一次工具。"""

    id: str
    name: str
    arguments: str  # 原始 JSON 字符串，故意不解析 —— 见模块 docstring


@dataclass(frozen=True)
class LLMResponse:
    """一次模型调用的结果。"""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    raw: Any = field(default=None, repr=False, compare=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议。真实实现走 litellm，测试实现按脚本返回。"""

    model: str

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> LLMResponse:
        """发一轮请求，返回模型的响应。"""
        ...


class LiteLLMClient:
    """真实实现：通过 litellm 调用模型。

    ``litellm`` 是**延迟导入**的：它导入较慢（约 2 秒），而测试只需要
    本模块的 dataclass 定义，不需要真的连模型。
    """

    def __init__(self, *, model: str, temperature: float = 0.0) -> None:
        # 离线价格表的开关在**模块级**设置 —— 见文件顶部。放在这里会太晚。
        import litellm

        self._litellm = litellm
        self.model = model
        self.temperature = temperature

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
        }
        # 没有工具时不要传 tools=[]，部分 provider 会报错
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        response = await self._litellm.acompletion(**kwargs)
        message = response.choices[0].message

        calls = tuple(
            ToolCall(
                id=call.id or f"call_{index}",
                name=call.function.name or "",
                arguments=call.function.arguments or "{}",
            )
            for index, call in enumerate(getattr(message, "tool_calls", None) or [])
        )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=getattr(message, "content", None),
            tool_calls=calls,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cost_usd=_estimate_cost(self._litellm, response),
            raw=response,
        )


def _estimate_cost(litellm_module: Any, response: Any) -> float:
    """估算本次调用花了多少钱。

    成本统计是 NFR-2 的判定依据，但它**不能让主流程崩掉** ——
    新模型可能不在 litellm 的价格表里，那时宁可为 0 也不要抛异常。
    """
    try:
        return float(litellm_module.completion_cost(completion_response=response) or 0.0)
    except Exception:  # noqa: BLE001 —— 成本估算失败不该影响诊断
        return 0.0


__all__ = ["LLMClient", "LLMResponse", "LiteLLMClient", "Message", "ToolCall"]
