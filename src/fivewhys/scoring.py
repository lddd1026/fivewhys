"""判分 —— 需求 §6.1 的实现。

## 为什么单独成一个模块

判分规则是整个项目的**核心资产**：没有它就没有「准确率」这个数字，
简历上那句话也就无从谈起。

所以它必须是被测试覆盖的正式模块，而不是塞在某个 demo 脚本里的一段代码。
M6 的评测台会直接复用这里。

## 判分规则（需求 §6.1）

==========  ======  ================================================
判定项      权重    比对方式
==========  ======  ================================================
根因服务    40%     精确字符串匹配（结构化，可精确比较）
故障类别    40%     枚举精确匹配（结构化，可精确比较）
根因描述    20%     关键词命中（脆弱，仅作辅助）
==========  ======  ================================================

总分 ≥ 80% 记为「判定为对」。

## ⚠️ 关键词只在两个字段里扫

只扫 ``root_cause`` 和 ``summary``，**绝不扫 ``ruled_out``**。

否则 agent 写「我**排除**了连接池问题」，会因为出现「连接池」而被误判为命中 ——
把「排除掉的假设」当成「得出的结论」，是判分里最容易犯的错。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fivewhys.models import Diagnosis, GroundTruth

# 需求 §6.1：总分 ≥ 80% 记为「判定为对」
PASS_THRESHOLD = 0.8

SERVICE_WEIGHT = 0.4
CATEGORY_WEIGHT = 0.4
KEYWORD_WEIGHT = 0.2


@dataclass(frozen=True)
class ScoreResult:
    """一次诊断的判分结果。"""

    total: float
    service_match: bool
    category_match: bool
    keyword_match: bool
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.total >= PASS_THRESHOLD

    def describe(self) -> str:
        return "；".join(self.notes) if self.notes else "完全正确"


def score_diagnosis(diagnosis: Diagnosis, truth: GroundTruth) -> ScoreResult:
    """按需求 §6.1 给一次诊断打分。"""
    notes: list[str] = []
    total = 0.0

    service_match = diagnosis.root_cause_service == truth.root_cause_service
    if service_match:
        total += SERVICE_WEIGHT
    else:
        notes.append(f"根因服务错（应为 {truth.root_cause_service}）")

    category_match = diagnosis.fault_category is truth.fault_category
    if category_match:
        total += CATEGORY_WEIGHT
    else:
        notes.append(f"故障类别错（应为 {truth.fault_category}）")

    # ⚠️ 只扫这两个字段 —— 绝不能把 ruled_out 算进来
    haystack = f"{diagnosis.root_cause} {diagnosis.summary}".lower()
    keyword_match = any(kw.lower() in haystack for kw in truth.match_keywords)
    if keyword_match:
        total += KEYWORD_WEIGHT
    else:
        notes.append("根因描述未命中判分关键词")

    return ScoreResult(
        total=round(total, 4),
        service_match=service_match,
        category_match=category_match,
        keyword_match=keyword_match,
        notes=notes,
    )


__all__ = [
    "CATEGORY_WEIGHT",
    "KEYWORD_WEIGHT",
    "PASS_THRESHOLD",
    "SERVICE_WEIGHT",
    "ScoreResult",
    "score_diagnosis",
]
