"""配置。

所有 `FIVEWHYS_*` 环境变量在这里统一收口，其它模块一律不直接读 os.environ。

为什么要这样：配置项散落在各处是 agent 项目失控的第一步 —— 你会不知道
「到底用的是哪个模型、哪个温度」跑出来的那组评测数字。

## 为什么这里要显式 load_dotenv（FIV-D2）

``SettingsConfigDict(env_file=".env")`` **只服务它自己的字段**（``FIVEWHYS_*`` 那些）。
``DEEPSEEK_API_KEY`` 不是 Settings 的字段 —— 它是 **litellm 从 ``os.environ`` 里读的**。

所以如果不显式把 ``.env`` 灌进进程环境，用户照着 README
「``cp .env.example .env`` 然后填上 key」做完，key 会被**静默忽略**，
然后收到一个莫名其妙的鉴权失败。这个 bug 是第一次拿真实 key 跑 demo 时发现的。

> ``override=False``（默认值）是有意的：真实环境变量优先于 ``.env``。
> CI 里注入的凭据、命令行临时导出的变量都应该压过本地文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# ⚠️ 必须在 Settings 被构造**之前**执行 —— 见模块 docstring。
# import fivewhys.config 就会触发它，所以 CLI / demo / 测试都覆盖到了。
load_dotenv(override=False)


class Settings(BaseSettings):
    """fivewhys 运行配置。字段名会自动加上 `FIVEWHYS_` 前缀读环境变量。"""

    model_config = SettingsConfigDict(
        env_prefix="FIVEWHYS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # litellm 的 "provider/model" 格式，例如 deepseek/deepseek-chat
    llm_model: str = "deepseek/deepseek-chat"

    # 自定义 API 端点。留空则用 provider 的官方地址。
    #
    # 两个用途：
    #   1. 指向自建的 OpenAI 兼容端点（vLLM / Ollama / 公司内网网关）
    #   2. 端到端测试时指向本地假 LLM 服务 —— 不开网络、不花钱，
    #      却能验证 HTTP 层、工具调用往返、判分、结论输出的整条链路
    api_base: str | None = None

    # 5 Whys 的最大追问层数 —— agent 的主要停止条件
    max_why_depth: int = 5

    # 单次诊断的成本上限（美元）。超过即中止。
    # 对应需求 NFR-2：硬上限 $0.10、平均目标 $0.03、全量评测（100 次）预算 $5。
    max_cost_usd: float = 0.10

    # 单次诊断最多多少轮工具调用（兜底，防止死循环）
    max_steps: int = 20

    # 单次 LLM 请求的超时（秒）。
    #
    # ⚠️ 这不是「优化」，是**有界性**：litellm 自己的默认超时是 600 秒，
    # 乘以 max_steps=20 就是最多 3 小时。provider 挂死（连上了、不回包）时，
    # 命令行会一直僵在那里，用户只能 Ctrl+C。
    # 上线前实测：一个「收了请求不回应」的假服务能让调用一直等下去（20 秒未返回）。
    llm_timeout_s: float = 60.0

    # 单次回复的最大输出 token 数。
    #
    # 上线前实测：我们原先**根本不发 max_tokens**，输出长度完全由 provider 决定。
    # 这是我们唯一能主动设的成本上限 —— 实测一个 8MB 的回复让**单次调用**
    # 记账 $0.84，是单次硬上限（$0.10）的 8 倍，而成本闸门是在调用**之后**才检查的。
    #
    # 2000 的依据：实测一次诊断的输出（含完整的 9 字段结论）约 600~900 token，
    # 留一倍余量；2000 token 输出成本约 $0.0008，乘以 20 步仍远低于 $0.10。
    max_output_tokens: int = 2000

    # ---- token 用量控制 ----
    #
    # 为什么在成本上限之外还要 token 上限：
    #
    # 1. **成本算不出来的时候，token 一定算得出来。** FIV-D3 那个 bug 就是
    #    `completion_cost` 查不到模型价格 → 成本恒为 0 → 成本上限形同虚设。
    #    token 是 provider 直接报的，不依赖价格表。
    # 2. **上下文会自己长大。** 每步都把工具返回追加进历史，20 步下来 prompt
    #    可能涨到几十万 token —— 那是「上下文溢出」，不是「花钱多」：
    #    provider 会直接报错，而那时已经白花了一堆钱。
    #
    # 单次诊断累计 token 上限（按 provider **实际上报**的 usage 累加）。
    # 实测一次 5 步诊断约 48k token —— 200k 够跑 4 倍长的调查，不会误伤。
    max_total_tokens: int = 200_000

    # 单次请求的上下文上限（**发出去之前**按字符估算，3 字符 ≈ 1 token）。
    #
    # 为什么要在发之前拦：deepseek-chat 的上下文窗口是 64k，
    # 超了 provider 直接报错 —— 与其等它报错，不如提前停，
    # 并且明确告诉人「是上下文涨满了」，而不是甩一个 provider 异常。
    # 换模型（窗口更大/更小）时改这个值。
    max_context_tokens: int = 60_000

    # 温度。0 是为了让诊断尽量可复现
    temperature: float = 0.0

    # ---- 证据来源校验（需求 FR-8）----
    #
    # 默认开着：一条**结论正确、过程编造**的诊断比「我查不出来」糟糕得多 ——
    # 它会让人相信一个没被验证过的推理，而 §6.1 的判分只看根因对不对。
    #
    # 为什么做成开关：M7 要测「结构化输出约束」到底带来多少提升，
    # 没有基线就说不清。这也是这个开关唯一正当的用途 —— 不是为了在生产里关掉它。
    verify_evidence: bool = True

    # ---- 轨迹落盘（需求 FR-9）----
    #
    # 默认**开着**：需求写的是「每次诊断**必须**落盘完整轨迹」，
    # 而「默认关掉、出事再开」等于永远拿不到出事的证据 ——
    # 上线前审查里那次没查明原因的失败就是这么来的。
    #
    # 测试里在 conftest.py 里设成 false（测试不该往仓库写运行时数据），
    # 要测轨迹本身的用例自己开（见 tests/test_trace.py）。
    trace_enabled: bool = True
    trace_root: Path = Path("runs")


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试里可以用 `get_settings.cache_clear()` 重置。"""
    return Settings()
