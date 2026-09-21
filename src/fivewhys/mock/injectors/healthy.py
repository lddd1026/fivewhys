"""正常场景 —— 用来测 agent 会不会「无中生有」。

## 为什么必须有它（需求 §10.2）

如果评测集全是「有故障」的场景，agent 会养成一个坏习惯：

    **无论输入什么，都要编出一个根因。**

因为环境一直在告诉它「总是有问题的」。这在实际使用中是灾难性的 ——
**一个永远报故障的诊断工具没人敢用。**

这个场景里系统完全健康：没有任何 ERROR、没有任何失败采样、指标全部正常。
正确答案是 :attr:`FaultCategory.NO_FAULT`。

**agent 答「没问题」才算对，答任何故障都是错。**

## 它也是评测集里的「对照组」

故障场景测的是「能不能找到问题」，正常场景测的是「会不会凭空造一个问题」。
两个指标一起看，才知道 agent 是真的在推理，还是在瞎猜。

面试时可以直接说：

    "我不只测它能不能找到故障，还测它会不会无中生有 ——
     所以评测集里混了 25% 的健康场景。"
"""

from __future__ import annotations

from datetime import timedelta

from fivewhys.mock.injectors import InjectionContext, register
from fivewhys.models import FaultCategory, GroundTruth

# 和别的故障保持同样的窗口长度，方便横向对比
DURATION = timedelta(minutes=5)

# 健康场景的请求间隔（秒）。和正常时期一致 —— 这样指标看起来就是"一切照常"
_REQUEST_INTERVAL_S = 0.5


@register(
    FaultCategory.NO_FAULT,
    "healthy",
    "系统完全健康，没有任何故障（用于测误报率）",
)
def inject(ctx: InjectionContext) -> GroundTruth:
    """什么都不注入 —— 这正是这个场景的全部意义。

    只生成和平时一模一样的正常流量。
    """
    start = ctx.at
    end = start + DURATION

    # 用系统的跨服务流量生成器，而不是手写 ——
    # 这样健康场景和服务真实运行时的样子完全一致，没有"人为痕迹"。
    cursor = start
    step = timedelta(seconds=_REQUEST_INTERVAL_S)
    while cursor < end:
        cursor = ctx.system.emit_request(cursor)
        cursor += step

    return GroundTruth(
        scenario_id=f"{ctx.target}-healthy-{start:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.NO_FAULT,
        # 问题里问的是哪个服务，就答哪个 —— 这一项在健康场景里本来就平凡
        root_cause_service=ctx.target,
        root_cause="系统在该时间窗口内完全健康，没有发现任何故障",
        injected_at=start,
        symptoms=[],
        # agent 用自然语言说"没问题"时应该出现的词
        match_keywords=[
            "无故障",
            "没有故障",
            "no fault",
            "healthy",
            "正常",
            "没有问题",
            "无需处理",
        ],
        # 健康场景没有"答案"可泄漏 —— 日志里本来就不该有故障信息
        answer_keywords=[],
    )


__all__ = ["DURATION", "inject"]
