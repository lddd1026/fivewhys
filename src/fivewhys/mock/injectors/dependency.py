"""下游服务 5xx —— 症状在上游，根因在下游。

## 这个场景考什么

**别被症状所在的服务骗了。**

问题问的是 order-service，报错的也是 order-service（入口服务）。但根因在
inventory-service —— 它的数据库地址被配错了。

agent 必须走出这条路径::

    order-service 日志： "inventory-service responded 503"
      -> 转去查 inventory-service
        inventory 日志： "stock-db connection refused"
          -> 查 inventory 的配置
            configs:      stock_db.url 被改成了错的地址     <- 根因

**只盯着问题里提到的那个服务，是找不到根因的。** 这正是分布式排障的核心难点，
也是这个场景和 db_pool 最重要的区别：db_pool 的根因就在被问的服务上。

## 为什么把根因配置放在下游

如果根因也在 order-service，那所有场景就都变成「在同一条日志流里找异常」，
考不出**跨服务追踪**这个能力。

配置放在下游，agent 就必须先跨过服务边界。
"""

from __future__ import annotations

from datetime import timedelta

from fivewhys.mock.injectors import InjectionContext, register
from fivewhys.mock.injectors._base import FaultScript, record_config_change
from fivewhys.models import FaultCategory, GroundTruth

DURATION = timedelta(minutes=5)
NOISE_INTERVAL_S = 3.0

CULPRIT = "inventory-service"
HEALTHY_DB_URL = "postgres://stock-db:5432/stock"
BROKEN_DB_URL = "postgres://stock-db-dr:5432/stock"
CONFIG_RELOAD_NOTE = "config reloaded from /etc/inventory-service/app.yaml"


@register(
    FaultCategory.DEPENDENCY_5XX,
    "dependency-5xx",
    "下游服务的依赖配错导致它整体 5xx，症状出现在上游入口服务",
)
def inject(ctx: InjectionContext) -> GroundTruth:
    order = ctx.system.service(ctx.target)
    culprit = ctx.system.service(CULPRIT)
    rng = ctx.rng

    order_script = FaultScript(service=order, metrics=ctx.metrics)
    culprit_script = FaultScript(service=culprit, metrics=ctx.metrics)

    start = ctx.at
    end = start + DURATION
    triggered_at = start - timedelta(seconds=2)

    # ---- 根因：下游服务的数据库地址被配错 ----
    culprit_script.info(triggered_at, CONFIG_RELOAD_NOTE)
    record_config_change(
        ctx.configs,
        service_name=CULPRIT,
        changes={"stock_db.url": BROKEN_DB_URL},
        at=triggered_at,
        note=CONFIG_RELOAD_NOTE,
    )

    # ---- 故障期间 ----
    cursor = start
    while cursor < end:
        trace = order.new_trace_id()

        # 上游：症状。它只知道"下游返回了 503"，不知道下游为什么坏。
        order_script.error(
            cursor,
            f"{CULPRIT} responded 503 Service Unavailable",
            trace,
            latency_ms=rng.randint(180, 320),
            status=503,
        )
        cursor += timedelta(seconds=rng.randint(4, 18))

        # 下游：根因线索。它自己知道是数据库连不上。
        culprit_script.error(
            cursor,
            "stock-db healthcheck failed: connection refused",
            trace,
            latency_ms=rng.randint(8, 25),
            status=503,
        )
        cursor += timedelta(seconds=rng.randint(2, 9))

        # 业务影响
        if rng.random() < 0.35:
            latency_ms = rng.randint(700, 1500)
            order_script.warn(
                cursor,
                f"request latency {latency_ms}ms exceeds SLO 500ms",
            )
            cursor += timedelta(seconds=rng.randint(2, 8))

    # ---- 背景噪声：两个服务都要有正常流量，否则"这个服务全是错误"太显眼 ----
    order_script.background_traffic(start, end, rng=rng, interval_s=NOISE_INTERVAL_S)
    culprit_script.background_traffic(
        start,
        end,
        rng=rng,
        path="/api/v1/reserve",
        interval_s=NOISE_INTERVAL_S * 2,
    )

    order_script.flush()
    culprit_script.flush()

    return GroundTruth(
        scenario_id=f"{ctx.target}-dependency-5xx-{start:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.DEPENDENCY_5XX,
        # ⚠️ 根因服务是【下游】，不是被问的那个
        root_cause_service=CULPRIT,
        root_cause=(
            f"{CULPRIT} 的 stock_db.url 被配置变更改成了不可达的地址"
            f"（{HEALTHY_DB_URL} -> {BROKEN_DB_URL}），"
            f"它开始整体返回 503，连锁导致 {ctx.target} 的请求失败"
        ),
        injected_at=start,
        symptoms=[
            f"{ctx.target} 出现 503，错误信息指向 {CULPRIT}",
            f"{CULPRIT} 自身报 stock-db connection refused",
            "请求延迟超 SLO，但远低于连接池耗尽那种 3 秒级",
        ],
        match_keywords=[
            "inventory",
            "stock-db",
            "stock_db",
            "503",
            "下游",
            "dependency",
            "依赖",
        ],
        # ⚠️ 只放**配置里才有**的词。日志只说"connection refused"（连不上），
        # 不说**为什么**连不上 —— 查配置才知道地址被改错了。
        answer_keywords=[
            "stock_db.url",
            "stock-db-dr",
            "地址配错",
            "url 被改",
        ],
    )


__all__ = ["BROKEN_DB_URL", "CULPRIT", "DURATION", "HEALTHY_DB_URL", "inject"]
