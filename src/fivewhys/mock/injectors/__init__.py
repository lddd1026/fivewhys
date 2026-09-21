"""故障注入器 —— 统一的接口与注册表。

## 为什么需要这一层

写第二种故障时你会发现：每种故障的骨架都一样 —— 收事件、排序、写日志、
同步记指标。真正不同的只有中间那几十行「描述现象」。

这一层把骨架固定下来，让每个注入器只写自己的那几十行。

## 怎么加一种新故障

::

    @register(FaultCategory.CERT_EXPIRED, "cert-expired", "证书过期")
    def inject(ctx: InjectionContext) -> GroundTruth:
        script = FaultScript(service=ctx.service, metrics=ctx.metrics)
        # ... 描述现象 ...
        script.flush()
        return GroundTruth(...)

然后在 :mod:`fivewhys.mock.injectors` 底部 import 一下这个模块即可。

## 一条设计纪律

**日志是现象，答案在配置里。**（见 :mod:`fivewhys.mock.changes` 的说明）

写新注入器时，不要让日志里出现 ``GroundTruth.answer_keywords`` ——
:meth:`fivewhys.scenario.Scenario.validate` 会自动检查这一点。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fivewhys.models import FaultCategory, GroundTruth

if TYPE_CHECKING:
    from fivewhys.mock.changes import ConfigStore, DeployStore
    from fivewhys.mock.logstore import LogStore
    from fivewhys.mock.metrics import MetricStore
    from fivewhys.mock.service import MockService
    from fivewhys.mock.topology import MockSystem


@dataclass
class InjectionContext:
    """一次故障注入所需的全部上下文。

    注入器不需要知道日志/指标/配置仓库是怎么拼起来的，也不用管自己在
    哪个场景里 —— 它只面对这一份上下文。
    """

    system: MockSystem
    at: datetime
    target: str
    seed: int = 0

    # ---- 便捷访问 ----

    @property
    def logs(self) -> LogStore:
        return self.system.store

    @property
    def metrics(self) -> MetricStore:
        return self.system.metrics

    @property
    def configs(self) -> ConfigStore:
        return self.system.configs

    @property
    def deploys(self) -> DeployStore:
        return self.system.deploys

    @property
    def service(self) -> MockService:
        return self.system.service(self.target)

    @property
    def rng(self) -> random.Random:
        """服务的随机源。

        ⚠️ 一定用它，不要用全局 ``random`` —— 场景必须可复现（NFR-1）。
        """
        return self.service.rng


@dataclass(frozen=True)
class InjectorSpec:
    """一种故障的注册信息。"""

    category: FaultCategory
    name: str
    description: str
    inject: Callable[[InjectionContext], GroundTruth]


INJECTORS: dict[FaultCategory, InjectorSpec] = {}


def register(
    category: FaultCategory,
    name: str,
    description: str,
) -> Callable[[Callable[[InjectionContext], GroundTruth]], Callable[..., GroundTruth]]:
    """把函数注册成某种故障的注入器。"""

    def decorator(
        fn: Callable[[InjectionContext], GroundTruth],
    ) -> Callable[..., GroundTruth]:
        if category in INJECTORS:
            raise ValueError(f"故障类别重复注册：{category}")
        INJECTORS[category] = InjectorSpec(
            category=category,
            name=name,
            description=description,
            inject=fn,
        )
        return fn

    return decorator


def get(category: FaultCategory) -> InjectorSpec:
    if category not in INJECTORS:
        known = ", ".join(sorted(c.value for c in INJECTORS)) or "（一个都没有）"
        raise KeyError(f"没有注册故障「{category}」。已注册：{known}")
    return INJECTORS[category]


def inject(category: FaultCategory, ctx: InjectionContext) -> GroundTruth:
    """按类别注入故障 —— 评测台和构造器都走这个入口。"""
    return get(category).inject(ctx)


def available() -> list[FaultCategory]:
    return sorted(INJECTORS, key=lambda category: category.value)


def catalogue() -> list[dict[str, Any]]:
    """给人（和未来的 CLI）看的故障清单。"""
    return [
        {
            "category": spec.category.value,
            "name": spec.name,
            "description": spec.description,
        }
        for spec in (INJECTORS[category] for category in available())
    ]


__all__ = [
    "INJECTORS",
    "InjectionContext",
    "InjectorSpec",
    "available",
    "catalogue",
    "get",
    "inject",
    "register",
]

# 在这里 import 各个注入器模块，触发它们的 @register。
# 放在文件末尾是必须的 —— 此时上面的 register / InjectionContext 已经定义好了。
from fivewhys.mock.injectors import db_pool  # noqa: E402, F401
