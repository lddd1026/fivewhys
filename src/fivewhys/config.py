"""配置。

所有 `FIVEWHYS_*` 环境变量在这里统一收口，其它模块一律不直接读 os.environ。

为什么要这样：配置项散落在各处是 agent 项目失控的第一步 —— 你会不知道
「到底用的是哪个模型、哪个温度」跑出来的那组评测数字。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # 温度。0 是为了让诊断尽量可复现
    temperature: float = 0.0


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试里可以用 `get_settings.cache_clear()` 重置。"""
    return Settings()
