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

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

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
    # provider **实际上服务**的那个模型名。
    #
    # 为什么它和 ``LiteLLMClient.model`` 是两个东西：前者是你请求的别名，
    # 后者是对方回给你的 id。实测请求 ``deepseek/deepseek-chat``，
    # DeepSeek 回的是 ``deepseek-flash``。
    #
    # 约束 C-7 要求「所有对外展示的数字必须标注所用模型」——
    # 只标请求名不够：同一份报告里「deepseek-chat」和实际跑的
    # 「deepseek-flash」是两回事，读者有权知道真正跑的是什么。
    # 这个字段就是那个「真正跑的」。（它也是 FIV-D3 成本算成 0 的成因。）
    served_model: str | None = None
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

    def __init__(
        self,
        *,
        model: str,
        temperature: float = 0.0,
        api_base: str | None = None,
        timeout_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        # 离线价格表的开关在**模块级**设置 —— 见文件顶部。放在这里会太晚。
        import litellm

        # litellm 在出错时会用 print 打一段 "Give Feedback / Get Help" 横幅
        # （不走 logging，所以压 logger 级别没用）。实测：坏 key 时它会夹在
        # 我们的错误行和结果表格之间，看起来像是我们的程序坏了。
        litellm.suppress_debug_info = True

        self._litellm = litellm
        self.model = model
        self.temperature = temperature
        self.api_base = api_base
        # 为什么必须显式给：litellm 默认 600 秒/次，× max_steps(20) = 最多 3 小时。
        # provider 挂死时命令行会一直僵着 —— 这是**有界性**问题，不是性能问题。
        self.timeout_s = timeout_s
        # 这是我们唯一能主动设的成本上限。不发它，输出长度完全由 provider 决定：
        # 实测一个 8MB 回复记了 $0.84，是单次硬上限（$0.10）的 8 倍
        # —— 而成本闸门是在调用**之后**才检查的，拦不住这一刀。
        self.max_output_tokens = max_output_tokens

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
        # 只在设置了才传：传 None 会让部分 provider 报错
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.timeout_s is not None:
            kwargs["timeout"] = self.timeout_s
        if self.max_output_tokens is not None:
            kwargs["max_tokens"] = self.max_output_tokens
        # 没有工具时不要传 tools=[]，部分 provider 会报错
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        response = await self._litellm.acompletion(**kwargs)
        message = response.choices[0].message

        content = getattr(message, "content", None)
        calls = tuple(
            ToolCall(
                id=call.id or f"call_{index}",
                name=call.function.name or "",
                arguments=call.function.arguments or "{}",
            )
            for index, call in enumerate(getattr(message, "tool_calls", None) or [])
        )

        _assert_response_is_sane(content=content, calls=calls)

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=content,
            tool_calls=calls,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cost_usd=_estimate_cost(self._litellm, response),
            served_model=getattr(response, "model", None),
            raw=response,
        )


class OversizedResponseError(RuntimeError):
    """一次回复大得不正常 —— 拒绝把它带进上下文。

    为什么宁可报错也不截断：这个回复会被**原样加进对话历史**，下一次请求
    再把它整个发回去。一份 8MB 的回复会让后续每一次调用的输入都多 8MB，
    成本是**平方级**增长的。

    上线前实测：8MB 回复让单次调用记账 **$0.84**，是单次硬上限（$0.10）的 8 倍，
    而成本闸门是在调用之后才检查的 —— 拦不住。报错会变成一次 `error` 记录
    （需求 §6.2 里这类不计入准确率分母），代价可控。
    """


# 一次回复的字符上限。正常的诊断结论是**千字级**（9 个字段 + 5 层追问），
# 100k 字符已经是它的 100 倍 —— 越过这条线必然是异常，不是模型话多。
MAX_RESPONSE_CHARS_ALLOWED = 100_000


# --------------------------------------------------------------------------
# token 估算（**只是估算**，硬指标用 provider 报的 usage）
# --------------------------------------------------------------------------

# 一个 token 大约几个字符。英文约 4，中文约 1.5，代码与日志混排实测接近 3。
#
# 为什么不用 tiktoken 精确算：它首次使用要**联网下载编码表**，而 FR-14a 要求
# 「无需 API Key、离线也能跑通自检」。为了一个预算估算引入联网依赖不划算 ——
# 与 tools/_render.py 的 MAX_RESPONSE_CHARS 是同一个理由、同一个比例。
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """按字符数粗略估 token。**只用于「发出去之前」的预检。**

    真正记账的数字一律用 provider 返回的 ``usage``：那是硬指标，这是估算。
    两者用途不同，不要混：估算用来「提前拦」，usage 用来「事后算」。
    """
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_request_tokens(
    messages: Sequence[Message],
    tools: Sequence[dict[str, Any]],
) -> int:
    """估一次请求的输入 token —— 在**发出去之前**判断上下文会不会撑爆。

    为什么要算这个：每步都把工具返回追加进历史，prompt 会自己长大。
    20 步下来可能到几十万 token，provider 会直接报上下文超限 ——
    与其等它报错（那时钱已经花了），不如提前停，并说清是谁涨满了。
    """
    chars = 0
    for message in messages:
        chars += len(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            chars += len(str(call))
    chars += len(json.dumps(list(tools), ensure_ascii=False))
    return max(1, chars // CHARS_PER_TOKEN)


def _assert_response_is_sane(*, content: str | None, calls: tuple[ToolCall, ...]) -> None:
    """回复大小不合常理时，早点报错。

    为什么要自己兜一道：``max_tokens`` 是**请求侧**的请求，provider 可以不理它
    （自建端点、代理、兼容层都可能）。这是**响应侧**的兜底。
    """
    total = len(content or "") + sum(len(call.arguments) for call in calls)
    if total > MAX_RESPONSE_CHARS_ALLOWED:
        raise OversizedResponseError(
            f"模型这一次回复了 {total:,} 字符（上限 {MAX_RESPONSE_CHARS_ALLOWED:,}）—— "
            "拒绝继续，避免把它带进后续每一次请求"
        )


def _estimate_cost(litellm_module: Any, response: Any) -> float:
    """本次调用花了多少钱。

    成本统计是 NFR-2 的判定依据，但它**不能让主流程崩掉** ——
    算不出来时宁可为 0 也不要抛异常。

    ## ⚠️ 为什么先读 ``_hidden_params``，而不是直接 ``completion_cost()``（FIV-D3）

    ``litellm.completion_cost(completion_response=...)`` 会拿**响应里回的 model 名**
    去查价格表。而 provider 回的模型名未必等于你请求的那个别名 ——
    实测请求 ``deepseek/deepseek-chat`` 时，DeepSeek 回的是 ``deepseek-flash``，
    于是查表失败、抛异常，被这里的 ``except`` 吞掉，**成本永远是 0.0000**。

    而 litellm 在 ``response._hidden_params["response_cost"]`` 里**已经算好了**
    这一次调用的钱（它用的是请求时的别名）。先读它，才是对的。

    这个 bug 的后果不是「少算几厘钱」，是**一个要写进 README 的指标是假的**：
    「平均成本 $0.0000」看起来像不要钱，实际上每次都花了钱。
    只有拿真实 key 跑一次才会发现 —— 假 LLM 服务返回的 usage 也让成本算不出来，
    所以离线测试全绿。
    """
    hidden = getattr(response, "_hidden_params", None) or {}
    cost = hidden.get("response_cost")
    if cost is not None:
        return float(cost)

    try:
        return float(litellm_module.completion_cost(completion_response=response) or 0.0)
    except Exception:  # noqa: BLE001 —— 成本估算失败不该影响诊断
        logger.debug("算不出这次调用的成本，按 0 计", exc_info=True)
        return 0.0


__all__ = [
    "CHARS_PER_TOKEN",
    "estimate_request_tokens",
    "estimate_tokens",
    "MAX_RESPONSE_CHARS_ALLOWED",
    "LLMClient",
    "LLMResponse",
    "LiteLLMClient",
    "Message",
    "OversizedResponseError",
    "ToolCall",
]
