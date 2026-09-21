"""内存泄漏 —— 渐进式故障，和「某一刻突然坏掉」形成对比。

## 这个场景考什么

**趋势，而不是瞬间。**

db_pool 和 dependency 都是「某一刻开始坏」——有一个明确的时间跳变点。
内存泄漏不是：延迟慢慢爬升，堆占用一路上涨，最后 OOM 重启，然后循环。

如果 agent 只会找「什么时候突然变了」，它会一无所获 ——
因为**根本没有那个时刻**。它必须看趋势。

## 证据链

::

    14:02:00  WARN   heap usage 71% of limit, gc pause 560ms
    14:02:40  WARN   heap usage 76% of limit, gc pause 608ms
    14:03:20  WARN   heap usage 82% of limit, gc pause 656ms
    14:04:00  WARN   heap usage 87% of limit, gc pause 696ms
    14:04:20  ERROR  request timed out during gc pause
    14:04:40  WARN   heap usage 93% of limit, gc pause 744ms
    14:05:00  ERROR  service restarted: OOMKilled (heap 512MiB / limit 512MiB)
    14:05:04  WARN   service recovered, heap usage 12% of limit
    ...  然后循环

    配置历史  14:01:58  runtime.max_heap_mb: 2048 -> 512        <- 根因

## 根因是什么

堆上限被从 2048MiB 砍到 512MiB。应用本来就有一点泄漏（涨得很慢，
以前根本撑不到上限），上限一砍就撑不住了。

这条根因的好处：它解释了「为什么现在才出问题」——
**同样的代码跑了几个月都没事，改完配置当天就崩。**
"""

from __future__ import annotations

from datetime import timedelta

from fivewhys.mock.injectors import InjectionContext, register
from fivewhys.mock.injectors._base import FaultScript, record_config_change
from fivewhys.models import FaultCategory, GroundTruth

DURATION = timedelta(minutes=8)
RESTART_INTERVAL = timedelta(minutes=4)
SAMPLE_INTERVAL_S = 2.0

HEALTHY_HEAP_MB = 2048
BROKEN_HEAP_MB = 512
CONFIG_RELOAD_NOTE = "config reloaded from /etc/order-service/runtime.yaml"

# 堆占用从 12% 爬到 ~97% 就 OOM
_START_FRACTION = 0.12
_GROWTH = 0.85

# 超过这个占用率才值得记一条 WARN
_WARN_THRESHOLD = 0.70

# 接近上限时开始出现 GC 超时
_GC_TIMEOUT_THRESHOLD = 0.85


@register(
    FaultCategory.MEMORY_LEAK,
    "memory-leak",
    "堆上限被配置下调，原本无害的缓慢泄漏撑不住，服务周期性 OOM 重启",
)
def inject(ctx: InjectionContext) -> GroundTruth:
    service = ctx.service
    rng = ctx.rng
    script = FaultScript(service=service, metrics=ctx.metrics, system=ctx.system)

    start = ctx.at
    end = start + DURATION
    triggered_at = start - timedelta(seconds=2)

    # ---- 根因：堆上限被砍 ----
    script.info(triggered_at, CONFIG_RELOAD_NOTE)
    record_config_change(
        ctx.configs,
        service_name=service.name,
        changes={"runtime.max_heap_mb": BROKEN_HEAP_MB},
        at=triggered_at,
        note=CONFIG_RELOAD_NOTE,
    )

    # ---- 渐进式的现象 ----
    last_restart = start
    cursor = start
    step = timedelta(seconds=SAMPLE_INTERVAL_S)

    while cursor < end:
        progress = (cursor - last_restart) / RESTART_INTERVAL

        # 到顶了：OOM 重启。
        # ⚠️ 刻意不报堆上限的**数值** —— 那个数字藏在配置里，
        # agent 必须去查才知道它被从 2048 砍到了 512。
        if progress >= 1.0:
            script.error(
                cursor,
                "service restarted: OOMKilled",
                service.new_trace_id(),
                latency_ms=rng.randint(2000, 4000),
                status=503,
            )
            cursor += timedelta(seconds=4)
            script.warn(cursor, "service recovered, heap usage 12% of limit")
            last_restart = cursor
            cursor += timedelta(seconds=rng.randint(30, 60))
            continue

        heap = _START_FRACTION + progress * _GROWTH
        base = service.base_latency_ms
        # 延迟随堆占用上升 —— GC 越来越频繁
        latency = max(1, int(rng.gauss(base * (1 + 4 * progress), base * 0.25)))

        script.ok(
            cursor,
            f"GET /api/v1/orders/{rng.randint(100000, 999999)} 200 {latency}ms",
            latency,
            trace_id=service.new_trace_id(),
        )

        if heap >= _WARN_THRESHOLD:
            script.warn(
                cursor,
                f"heap usage {heap:.0%} of limit, gc pause {int(heap * 800)}ms",
            )

        # 接近上限时开始出现 GC 超时
        if heap >= _GC_TIMEOUT_THRESHOLD and rng.random() < 0.15:
            script.error(
                cursor,
                "request timed out during gc pause",
                service.new_trace_id(),
                latency_ms=rng.randint(2500, 3500),
                status=504,
            )

        cursor += step

    # ---- 背景噪声：整个系统照常有流量（FIV-D1）----
    # ⚠️ 这一段是 FIV-D1 补上的：这个注入器原来只有上面那些「堆占用逐步上升」
    # 的采样，故障窗口里别的服务一条数据都没有 —— 那本身就是「有故障」的信号。
    # 跨服务流量走的是正常延迟，所以 P95 的上升趋势仍然由上面的采样主导演示。
    script.system_traffic(start, end, rng=rng)

    script.flush()

    return GroundTruth(
        scenario_id=f"{service.name}-memory-leak-{start:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.MEMORY_LEAK,
        root_cause_service=service.name,
        root_cause=(
            f"{service.name} 的堆上限被配置变更下调"
            f"（{HEALTHY_HEAP_MB}MiB -> {BROKEN_HEAP_MB}MiB）。"
            "应用本来就存在缓慢的内存泄漏，以前撑不到上限，"
            "上限一砍就撑不住了，于是周期性 OOM 重启"
        ),
        injected_at=start,
        symptoms=[
            "延迟**逐渐**爬升，没有突然的跳变点",
            "heap usage 从 70% 一路涨到 90% 以上",
            "gc pause 越来越长，后期出现 gc 超时导致的请求失败",
            "服务周期性 OOMKilled 重启，重启后堆占用回到 12%",
        ],
        match_keywords=[
            "heap",
            "oom",
            "memory",
            "内存",
            "泄漏",
            "leak",
            "gc",
        ],
        # ⚠️ 只放**配置里才有**的词。日志只说 "heap usage 93% of limit" 和
        # "OOMKilled" —— 不说 limit 具体是多少，也不说它被改过。
        answer_keywords=[
            "max_heap_mb",
            "堆上限",
            "heap limit",
            str(HEALTHY_HEAP_MB),
        ],
    )


__all__ = [
    "BROKEN_HEAP_MB",
    "DURATION",
    "HEALTHY_HEAP_MB",
    "RESTART_INTERVAL",
    "inject",
]
