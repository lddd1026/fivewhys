"""判分模块的测试 —— 需求 §6.1。

判分规则是整个项目的核心资产：它错了，「准确率」这个数字就没有意义，
简历上那句话也就不成立。所以它必须有测试。

对应需求：§6.1（判定项与权重）、§6.2（这个模块不负责分母，见 M6）
"""

from __future__ import annotations

from datetime import UTC, datetime

from fivewhys.models import Confidence, Diagnosis, Evidence, FaultCategory, GroundTruth, WhyStep
from fivewhys.scoring import PASS_THRESHOLD, ScoreResult, score_diagnosis

TRUTH = GroundTruth(
    scenario_id="s1",
    fault_category=FaultCategory.DB_POOL_EXHAUSTED,
    root_cause_service="order-service",
    root_cause="连接池上限被下调",
    injected_at=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
    symptoms=["错误率飙升"],
    match_keywords=["connection", "pool", "连接池", "耗尽"],
    answer_keywords=["pool", "连接池"],
)


def _diagnosis(**overrides: object) -> Diagnosis:
    base: dict[str, object] = {
        "root_cause": "连接池上限被配置变更下调，导致连接耗尽",
        "root_cause_service": "order-service",
        "fault_category": FaultCategory.DB_POOL_EXHAUSTED,
        "confidence": Confidence.HIGH,
        "why_chain": [WhyStep(depth=1, question="为什么失败？", answer="等连接超时")],
        "evidence": [Evidence(source="query_logs", finding="connection wait time 飙升")],
        "ruled_out": ["下游服务无异常"],
        "suggested_fix": "回滚配置",
        "summary": "连接池耗尽导致超时",
    }
    base.update(overrides)
    return Diagnosis(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 权重
# --------------------------------------------------------------------------


def test_fully_correct_scores_one() -> None:
    result = score_diagnosis(_diagnosis(), TRUTH)
    assert result.total == 1.0
    assert result.passed
    assert result.service_match and result.category_match and result.keyword_match
    assert result.notes == []


def test_correct_service_and_category_scores_eighty_percent() -> None:
    """服务对 + 类别对 = 80%，正好踩在判定线上。"""
    result = score_diagnosis(_diagnosis(root_cause="别的原因", summary="没关键词"), TRUTH)
    assert result.total == 0.8
    assert result.passed, "80% 应该算通过（需求 §6.1 是 ≥ 80%）"
    assert result.keyword_match is False


def test_wrong_service_and_category_scores_only_keyword_weight() -> None:
    result = score_diagnosis(
        _diagnosis(root_cause_service="payment-service", fault_category=FaultCategory.SLOW_QUERY),
        TRUTH,
    )
    assert result.total == 0.2
    assert not result.passed
    assert len(result.notes) == 2


def test_wrong_everything_scores_zero() -> None:
    result = score_diagnosis(
        _diagnosis(
            root_cause="磁盘满了",
            root_cause_service="payment-service",
            fault_category=FaultCategory.DISK_FULL,
            summary="磁盘问题",
        ),
        TRUTH,
    )
    assert result.total == 0.0
    assert not result.passed


def test_missing_service_alone_drops_below_threshold() -> None:
    """只错服务（40%）→ 60%，不通过。"""
    result = score_diagnosis(_diagnosis(root_cause_service="payment-service"), TRUTH)
    assert result.total == 0.6
    assert not result.passed


# --------------------------------------------------------------------------
# ⭐ 最容易写错的一条：ruled_out 绝不能参与判分
# --------------------------------------------------------------------------


def test_ruled_out_never_counts_as_a_hit() -> None:
    """agent 写「我【排除】了连接池问题」不能被判成答对。

    这是判分里最容易犯的错：把「排除掉的假设」当成「得出的结论」。
    """
    result = score_diagnosis(
        _diagnosis(
            root_cause="下游服务响应慢",
            summary="疑似网络抖动",
            ruled_out=["连接池耗尽（已排除，等待时间正常）", "pool exhausted 已排除"],
        ),
        TRUTH,
    )
    assert result.keyword_match is False, "ruled_out 里的关键词不能被算作命中"
    assert result.total == 0.8, "服务+类别对，但根因描述不该得分"


def test_keyword_hit_only_needs_root_cause_or_summary() -> None:
    """只在 root_cause 里命中也算 —— 不要求 summary 也命中。"""
    result = score_diagnosis(
        _diagnosis(root_cause="连接池被耗尽", summary="看日志"),
        TRUTH,
    )
    assert result.keyword_match is True
    assert result.total == 1.0


def test_keyword_matching_is_case_insensitive() -> None:
    result = score_diagnosis(_diagnosis(root_cause="POOL EXHAUSTED", summary="x"), TRUTH)
    assert result.keyword_match is True


# --------------------------------------------------------------------------
# 判定线
# --------------------------------------------------------------------------


def test_threshold_is_eighty_percent() -> None:
    assert PASS_THRESHOLD == 0.8


def test_score_result_is_immutable_and_describable() -> None:
    result = score_diagnosis(_diagnosis(root_cause_service="payment-service"), TRUTH)
    assert isinstance(result, ScoreResult)
    assert "根因服务错" in result.describe()

    ok = score_diagnosis(_diagnosis(), TRUTH)
    assert ok.describe() == "完全正确"
