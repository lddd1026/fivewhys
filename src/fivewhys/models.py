"""fivewhys 的核心数据模型 —— 整个项目的地基。

这里有三组模型，分别对应三个不同的问题：

1. `Diagnosis`    —— agent 最终要交出来的结构化结论。
                     用 Pydantic 约束它，是为了让「模型自由发挥的文字」
                     变成「可以自动判分的对象」。这是 M7 提升准确率的起点。

2. `GroundTruth`  —— 注入故障时记录的正确答案。
                     没有它就没有评测，整个项目会退化成 demo。

3. `AgentRun`     —— 一次诊断的完整轨迹。
                     成本、延迟、工具调用次数、失败原因全在这里，
                     是你写简历时那些数字的唯一来源。

设计原则：日志里只允许出现「现象」，绝不允许直接出现答案。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class FaultCategory(StrEnum):
    """故障类别。

    故意做成可枚举的：自动判分要靠它，做成自由文本就没法比对了。
    M3 会把这里扩到 20 种。
    """

    DB_POOL_EXHAUSTED = "db_pool_exhausted"
    SLOW_QUERY = "slow_query"
    MEMORY_LEAK = "memory_leak"
    DEPENDENCY_5XX = "dependency_5xx"
    CACHE_MISS_STORM = "cache_miss_storm"
    BAD_CONFIG_ROLLOUT = "bad_config_rollout"
    CERT_EXPIRED = "cert_expired"
    DISK_FULL = "disk_full"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --------------------------------------------------------------------------
# 观测数据
# --------------------------------------------------------------------------


class LogEntry(BaseModel):
    """一条日志。"""

    ts: datetime
    service: str
    level: LogLevel
    message: str
    trace_id: str | None = None


# --------------------------------------------------------------------------
# agent 的输出
# --------------------------------------------------------------------------


class Evidence(BaseModel):
    """一条支撑结论的证据。

    硬性要求：必须来自某次真实的工具返回，不能是模型编造的。
    M5 会加校验：`source` 必须能在 trace 里找到对应记录。
    """

    source: str = Field(description="证据来自哪次工具调用，例如 query_logs(order-service)")
    finding: str = Field(description="观察到了什么")
    supports: bool = Field(default=True, description="True=支持结论，False=用于排除嫌疑")


class WhyStep(BaseModel):
    """5 Whys 链条中的一层。"""

    depth: int = Field(ge=1, le=10, description="第几层追问，从 1 开始")
    question: str = Field(description="追问，形如「为什么 X 会发生？」")
    answer: str = Field(description="这一步得出的答案")
    evidence: list[Evidence] = Field(default_factory=list)


class Diagnosis(BaseModel):
    """agent 的最终结构化输出 —— 本项目的核心交付物。"""

    root_cause: str = Field(description="根因的一句话描述")
    root_cause_service: str = Field(description="根因所在的服务名")
    fault_category: FaultCategory
    confidence: Confidence
    why_chain: list[WhyStep] = Field(default_factory=list, description="5 Whys 追问链")
    evidence: list[Evidence] = Field(default_factory=list)
    ruled_out: list[str] = Field(
        default_factory=list,
        description="被排除的嫌疑及排除理由 —— 这一步最能体现推理过程",
    )
    suggested_fix: str = Field(default="", description="建议的修复动作")
    summary: str = Field(default="", description="给值班工程师看的简短总结")


# --------------------------------------------------------------------------
# 评测
# --------------------------------------------------------------------------


class GroundTruth(BaseModel):
    """注入故障时写下的正确答案，用来做自动判分。"""

    scenario_id: str
    fault_category: FaultCategory
    root_cause_service: str
    root_cause: str
    injected_at: datetime
    symptoms: list[str] = Field(description="注入后系统表面能看到的现象")
    match_keywords: list[str] = Field(
        description="判分用的关键词。agent 的结论里命中才算对",
    )


class ToolCallRecord(BaseModel):
    """一次工具调用的记录。"""

    step: int
    tool: str
    args: dict[str, Any]
    ok: bool
    result_summary: str = ""
    error: str | None = None
    latency_ms: int = 0


class AgentRun(BaseModel):
    """一次完整诊断的轨迹。"""

    scenario_id: str
    model: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    steps: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    stop_reason: str = Field(
        default="unknown",
        description="为什么停下来：submitted / max_steps / max_cost / error",
    )

    @property
    def duration_s(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()
