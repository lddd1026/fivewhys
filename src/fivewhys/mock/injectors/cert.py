"""证书过期 —— 突发事件，而且只影响一条调用路径。

## 这个场景考什么

**范围。** 前面三种故障都是「服务整体变差」，这一种只打中**一条调用链**：

    调 payment-service 的请求全挂（TLS 握手失败）
    调 inventory-service 的请求完全正常

如果 agent 只看「错误率」，它会得出「服务整体在坏」；只有看到
**「坏的全是同一个下游」**，才能定位到证书。

## 证据链

::

    14:02:00  ERROR  payment-service call failed: x509: certificate has expired
                      or is not yet valid
    14:02:00  WARN   circuit breaker opened for payment-service
    14:02:03  ERROR  payment-service call failed: x509: certificate has expired
    ...   同时，inventory-service 的调用完全正常

    配置历史  14:01:58  tls.cert_not_after: 2026-01-01T14:02:00Z   <- 到期时刻

## 一个刻意留的坑

报错信息里同时有 ``certificate`` 和 ``payment-service``，
但**「谁的报告里出现 payment-service」和「谁是根因」是两件事** ——
根因服务是 order-service（它的证书过期了），不是 payment-service。

这考验的是：agent 会不会因为错误信息里老是出现 payment-service，
就把它当成根因服务。
"""

from __future__ import annotations

from datetime import timedelta

from fivewhys.mock.injectors import InjectionContext, register
from fivewhys.mock.injectors._base import FaultScript, record_config_change
from fivewhys.models import FaultCategory, GroundTruth

DURATION = timedelta(minutes=5)
NOISE_INTERVAL_S = 3.0

VICTIM_DEPENDENCY = "payment-service"
CONFIG_RELOAD_NOTE = "config reloaded from /etc/order-service/tls.yaml"


@register(
    FaultCategory.CERT_EXPIRED,
    "cert-expired",
    "调用某一个下游时用的客户端证书过期，只有那条调用链失败",
)
def inject(ctx: InjectionContext) -> GroundTruth:
    service = ctx.service
    victim = ctx.system.service(VICTIM_DEPENDENCY)
    rng = ctx.rng

    script = FaultScript(service=service, metrics=ctx.metrics)
    victim_script = FaultScript(service=victim, metrics=ctx.metrics)

    start = ctx.at
    end = start + DURATION
    triggered_at = start - timedelta(seconds=2)

    # ---- 根因：证书到期时间被改到了"现在" ----
    script.info(triggered_at, CONFIG_RELOAD_NOTE)
    record_config_change(
        ctx.configs,
        service_name=service.name,
        changes={
            "tls.cert_not_after": f"{start:%Y-%m-%dT%H:%M:%SZ}",
            "tls.client_cert": "/etc/order-service/tls/client.pem",
        },
        at=triggered_at,
        note=CONFIG_RELOAD_NOTE,
    )

    # ---- 故障期间：只有打给 victim 的调用失败 ----
    cursor = start
    while cursor < end:
        trace = service.new_trace_id()

        # ⚠️ 刻意不写"证书过期"。
        #
        # 真实系统当然会打印 x509 的具体原因，但那样一来 agent 只要 grep
        # "certificate" 就完事了 —— 我们的设计纪律是「日志给症状、配置藏答案」。
        #
        # "TLS 握手失败"只说出了问题出在哪一层，**不说是为什么**：
        # 可能是证书过期、可能是对方不信任我们、也可能是协议版本不匹配。
        # agent 必须去查证书配置才能确定。
        script.error(
            cursor,
            f"{VICTIM_DEPENDENCY} call failed: TLS handshake error",
            trace,
            latency_ms=rng.randint(120, 260),
            status=502,
        )
        cursor += timedelta(seconds=rng.randint(3, 12))

        # 熔断器打开 —— 这是"只有一条路径受影响"的强信号
        if rng.random() < 0.25:
            script.warn(cursor, f"circuit breaker opened for {VICTIM_DEPENDENCY}")
            cursor += timedelta(seconds=rng.randint(2, 6))

        # 业务影响
        if rng.random() < 0.3:
            latency_ms = rng.randint(600, 1200)
            script.warn(cursor, f"request latency {latency_ms}ms exceeds SLO 500ms")
            cursor += timedelta(seconds=rng.randint(2, 6))

    # ---- 背景噪声 ----
    # ⚠️ 两条路径都要有正常流量，才能体现「只有一条坏了」。
    # 上游自己的入站请求
    script.background_traffic(start, end, rng=rng, interval_s=NOISE_INTERVAL_S)
    # 被打中的那个下游：它自己是**健康的**，请求正常处理，只是上游连不上它
    victim_script.background_traffic(
        start,
        end,
        rng=rng,
        path="/api/v1/charge",
        interval_s=NOISE_INTERVAL_S * 2,
    )
    # 没被影响的另一条路径 —— 用来做对照
    healthy_script = FaultScript(
        service=ctx.system.service("inventory-service"),
        metrics=ctx.metrics,
    )
    healthy_script.background_traffic(
        start,
        end,
        rng=rng,
        path="/api/v1/reserve",
        interval_s=NOISE_INTERVAL_S * 2,
    )

    script.flush()
    victim_script.flush()
    healthy_script.flush()

    return GroundTruth(
        scenario_id=f"{service.name}-cert-expired-{start:%Y%m%d%H%M%S}",
        fault_category=FaultCategory.CERT_EXPIRED,
        # ⚠️ 根因是【发起调用的一方】的证书过期，不是被调用的 payment-service。
        # 错误信息里反复出现 payment-service，很容易被误导。
        root_cause_service=service.name,
        root_cause=(
            f"{service.name} 用于调用 {VICTIM_DEPENDENCY} 的客户端证书已过期"
            "（tls.cert_not_after 被改成当前时刻），"
            f"于是所有打给 {VICTIM_DEPENDENCY} 的调用 TLS 握手失败"
        ),
        injected_at=start,
        symptoms=[
            f"失败请求全部集中在打给 {VICTIM_DEPENDENCY} 的那条链路上",
            "错误信息只说 TLS 握手失败，不说是为什么",
            "熔断器被打开",
            "其他下游（inventory-service）完全正常 —— 范围很窄",
        ],
        match_keywords=[
            "cert",
            "证书",
            "tls",
            "x509",
            "expired",
            "过期",
            "握手",
        ],
        # ⚠️ 只放**配置里才有**的词。日志里绝不出现它们 ——
        # 日志只说"TLS 握手失败"，查配置才知道是证书到期时间到了。
        answer_keywords=[
            "cert_not_after",
            "client.pem",
            "证书有效期",
            "证书到期时间",
        ],
    )


__all__ = ["DURATION", "VICTIM_DEPENDENCY", "inject"]
