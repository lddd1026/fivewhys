"""坏配置发布 —— 根因要靠**发布历史**才能关联上。

## 这个场景考什么

**「配置变了」和「代码发布了」不是一回事。**

db_pool 的根因是一次**手动改配置**（没有发布记录）。
这一种的根因是一次**发布**（有发布记录），发布顺带改了配置。

两者的日志和配置历史长得很像，区别只在发布历史里:

    db_pool         配置历史 14:01:58  pool_size 50 -> 5     发布历史：无
    bad_rollout     配置历史 14:01:58  retry.max 3 -> 0      发布历史：v1.4.2 在 14:01:58

**agent 要能说出「这是发布引入的」**，而不是笼统地说「配置被改了」。
这个区别在真实排障里很重要 —— 它决定了处置方式是「回滚发布」还是「改回配置」。

## 证据链

::

    14:01:58  INFO   deploy started: order-service v1.4.2 by alice
    14:01:58  INFO   deploy finished: order-service v1.4.2 (released)
    14:02:00  ERROR  downstream call failed, no retry left: retry budget exhausted
    14:02:05  ERROR  downstream call failed, no retry left
    ...  错误信息里带 "retry" —— 指向重试策略

    发布历史  14:01:58  order-service v1.3.9 -> v1.4.2  <- 根因在这里
    配置历史  14:01:58  retry.max: 3 -> 0
"""

from __future__ import annotations

from datetime import timedelta

from fivewhys.mock.injectors import InjectionContext, register
from fivewhys.mock.injectors._base import (
    DEFAULT_TRAFFIC_INTERVAL_S,
    FaultScript,
    record_config_change,
)
from fivewhys.models import FaultCategory, GroundTruth

DURATION = timedelta(minutes=5)
NOISE_INTERVAL_S = DEFAULT_TRAFFIC_INTERVAL_S

PREVIOUS_VERSION = "v1.3.9"
BAD_VERSION = "v1.4.2"
RELEASER = "alice"
DEPLOY_NOTE = "deploy order-service v1.4.2 (canary -> full)"


@register(
    FaultCategory.BAD_CONFIG_ROLLOUT,
    "bad-config-rollout",
    "一次发布把重试次数改成了 0，下游偶发失败直接变成用户可见的失败",
)
def inject(ctx: InjectionContext) -> GroundTruth:
    service = ctx.service
    rng = ctx.rng
    script = FaultScript(service=service, metrics=ctx.metrics, system=ctx.system)

    start = ctx.at
    end = start + DURATION
    released_at = start - timedelta(seconds=2)

    # ---- 根因：一次发布，它顺带改掉了重试策略 ----
    script.info(released_at, f"deploy started: {service.name} {BAD_VERSION} by {RELEASER}")
    script.info(released_at, f"deploy finished: {service.name} {BAD_VERSION} (released)")

    if ctx.deploys is not None:
        ctx.deploys.add(
            released_at,
            service.name,
            version=BAD_VERSION,
            operator=RELEASER,
            note=DEPLOY_NOTE,
        )
    record_config_change(
        ctx.configs,
        service_name=service.name,
        changes={"retry.max": 0, "retry.backoff_ms": 0},
        at=released_at,
        note=f"applied by deploy {BAD_VERSION}",
    )

    # ---- 故障期间：下游偶发失败，但没有重试兜底，于是直接变成用户可见的失败 ----
    cursor = start
    while cursor < end:
        trace = service.new_trace_id()

        script.error(
            cursor,
            "downstream call failed, no retry left: retry budget exhausted",
            trace,
            latency_ms=rng.randint(400, 900),
            status=502,
        )
        cursor += timedelta(seconds=rng.randint(6, 20))

        if rng.random() < 0.3:
            # ⚠️ 不写 "retry.max=0" —— 那是答案，藏在配置里。
            # 这里只说"没有兜底"，agent 得自己去查配置才知道兜底为什么没了。
            script.warn(
                cursor,
                "upstream error surfaced to caller (no retry fallback)",
            )
            cursor += timedelta(seconds=rng.randint(2, 7))

        if rng.random() < 0.3:
            latency_ms = rng.randint(550, 1100)
            script.warn(cursor, f"request latency {latency_ms}ms exceeds SLO 500ms")
            cursor += timedelta(seconds=rng.randint(2, 6))

    script.system_traffic(start, end, rng=rng)
    # ⚠️ 走完整调用链，别用 background_traffic（见 _base.system_traffic，FIV-D1）
    script.flush()

    return GroundTruth(
        scenario_id=f"{service.name}-bad-rollout-{start:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.BAD_CONFIG_ROLLOUT,
        root_cause_service=service.name,
        root_cause=(
            f"发布 {BAD_VERSION} 把重试策略改成了 retry.max=0，"
            "下游的偶发失败失去了重试兜底，直接暴露成用户可见的错误"
        ),
        injected_at=start,
        symptoms=[
            "错误从发布时刻开始，之前完全正常",
            "错误信息提到 retry budget exhausted —— 指向重试策略",
            "下游本身没有异常，是上游不再重试",
        ],
        match_keywords=[
            "retry",
            "重试",
            "deploy",
            "发布",
            "rollout",
            "回滚",
        ],
        # ⚠️ 只放**配置里才有**的词。日志说 "retry budget exhausted"（没有重试额度了），
        # 不说额度为什么是 0 —— 查配置才知道 retry.max 被发布改成了 0。
        answer_keywords=[
            "retry.max",
            "retry.backoff_ms",
            "重试次数",
        ],
    )


__all__ = ["BAD_VERSION", "DURATION", "PREVIOUS_VERSION", "inject"]
