"""模型与 provider 的元信息 —— 让「换模型」这件事有据可依（需求 FR-6 / 约束 C-7）。

## 为什么需要这一层

「接入多模型」听起来只是改个配置项，其实有三个**只有真换过才知道**的坑：

1. **上下文窗口差得极远。** 实测（litellm 本地表，离线可查）：

   ======================  ============  =============
   模型                    输入窗口       输出上限
   ======================  ============  =============
   ``deepseek/deepseek-chat``  131,072       8,192
   ``gpt-4o-mini``           128,000      16,384
   ``gemini/gemini-2.0-flash`` 1,048,576     8,192
   ======================  ============  =============

   窗口差 8 倍。如果上下文闸门写死一个数（我们原先就是 60k），
   换到 gemini 上会**提前 17 倍停止**，把能跑完的调查活活掐掉。

2. **模型名写错要到第一次调用才发现。** 等到花钱那一刻才知道名字不对，太晚。
   现在可以在 ``doctor`` 里当场看出来。

3. **provider 回给你的模型名，未必是你请求的那个。** 实测：请求
   ``deepseek/deepseek-chat``，DeepSeek 端返回的 model id 是 ``deepseek-flash``。
   约束 C-7 要求「所有对外展示的数字必须标注所用模型」—— 只标请求名是不够的，
   得把**实际服务的模型**也记下来（见 :class:`fivewhys.agent.llm.LLMResponse`）。

## 为什么不去联网查

``litellm`` 的本地表里就带着窗口和价格，``LITELLM_LOCAL_MODEL_COST_MAP``
在 :mod:`fivewhys.agent.llm` 模块级设好了。查它不花钱、不联网、毫秒级 ——
符合 FR-14a 的离线要求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# provider 前缀 -> 该 provider 的 API Key 环境变量名。
#
# 收口到一处：原先 CLI 和 demo 各有一份，加一个 provider 要改两个地方 ——
# 而漏改的那一处不会报错，只会在用到时静默失效。
PROVIDER_API_KEYS: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure": "AZURE_API_KEY",
    "ollama": "OLLAMA_API_KEY",  # 本地推理通常不需要，但 litellm 会读它
}

# 查不到模型信息时的保守默认值。
#
# 为什么是 32k 而不是更大：窗口未知时，**猜大了会让 provider 报错**
# （那时钱已经花了），猜小了只是提前停一次、并且有明确的 stop_note 可查。
# 两害相权，宁可早停。
UNKNOWN_MODEL_CONTEXT_TOKENS = 32_000

# 用窗口的百分之多少当上下文闸门。
#
# 留 20% 不是保险起见，是因为三个东西都要从同一个窗口里出：
# 我们按字符估的 token 数会偏、模型这一轮还要输出（最多 max_output_tokens）、
# 以及 provider 自己也留了余量。贴着上限设等于把「提前拦住」
# 变成「看谁先报错」。
CONTEXT_SAFETY_RATIO = 0.8


@dataclass(frozen=True)
class ModelInfo:
    """一个模型的元信息。``known=False`` 表示 litellm 的本地表里没有它。"""

    model: str
    known: bool
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None

    @property
    def provider(self) -> str:
        """``provider/model`` 里的 provider 部分。"""
        return self.model.split("/", 1)[0]

    @property
    def api_key_env(self) -> str | None:
        return PROVIDER_API_KEYS.get(self.provider)

    def context_limit(self, *, override: int | None = None) -> int:
        """这个模型该用多大的上下文闸门。

        ``override`` 优先（用户说了算）；否则按窗口的
        :data:`CONTEXT_SAFETY_RATIO` 算；窗口也查不到就用保守默认值。
        """
        if override is not None:
            return override
        if self.max_input_tokens:
            return int(self.max_input_tokens * CONTEXT_SAFETY_RATIO)
        return UNKNOWN_MODEL_CONTEXT_TOKENS

    @property
    def input_price_per_million(self) -> float | None:
        if self.input_cost_per_token is None:
            return None
        return self.input_cost_per_token * 1_000_000

    @property
    def output_price_per_million(self) -> float | None:
        if self.output_cost_per_token is None:
            return None
        return self.output_cost_per_token * 1_000_000


def describe_model(model: str) -> ModelInfo:
    """查一个模型的元信息。**不抛异常** —— 查不到就返回 ``known=False``。

    为什么不让它抛：这个函数的调用方是 ``doctor`` 和预算计算，
    它们要的是「能不能用、窗口多大」，不是一个异常。
    把「未知」当成一种正常返回值，调用方才不会忘记处理它。
    """
    try:
        import litellm

        # ⚠️ 必须在**调用之前**关掉：模型名不认识时，litellm 会走
        # "LLM Provider NOT provided" 分支，用 `print()`（不是 logging）
        # 甩出两遍红色 "Provider List: https://docs.litellm.ai/docs/providers"。
        #
        # 这两行恰好出现在**最需要看清楚**的场景里：doctor --model 就是用来
        # 查「名字是不是写错了」的，结果我们的警告被两行库噪音夹在中间。
        # 与 llm.py 里关掉 "Give Feedback / Get Help" 横幅是同一个理由、同一个开关。
        litellm.suppress_debug_info = True

        # ⚠️ 标注成 Any 而不是 dict[str, Any]：litellm 的返回值是它自己的
        # TypedDict ``ModelInfo``，而 TypedDict **不能**赋给可变 dict 类型
        # （mypy 会报 assignment；实测）。它是外部库的私有形状，
        # 我们只按 key 取值、不依赖它的类型，所以在这一层把它当不透明的。
        raw: Any = litellm.get_model_info(model)
    except Exception:  # noqa: BLE001 —— 未知模型、litellm 内部变化都不该让诊断崩掉
        return ModelInfo(model=model, known=False)

    return ModelInfo(
        model=model,
        known=True,
        max_input_tokens=raw.get("max_input_tokens") or raw.get("max_tokens"),
        max_output_tokens=raw.get("max_output_tokens") or raw.get("max_tokens"),
        input_cost_per_token=raw.get("input_cost_per_token"),
        output_cost_per_token=raw.get("output_cost_per_token"),
    )


def api_key_env_for(model: str) -> str | None:
    """这个模型该从哪个环境变量读 key。"""
    return PROVIDER_API_KEYS.get(model.split("/", 1)[0])


__all__ = [
    "CONTEXT_SAFETY_RATIO",
    "PROVIDER_API_KEYS",
    "UNKNOWN_MODEL_CONTEXT_TOKENS",
    "ModelInfo",
    "api_key_env_for",
    "describe_model",
]
