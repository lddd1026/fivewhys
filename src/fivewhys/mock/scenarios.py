"""兼容层：保留 M1 时代的注入器入口。

M3 起，故障注入器统一搬到了 :mod:`fivewhys.mock.injectors` ——
每种故障一个模块，通过注册表按类别取用：

::

    from fivewhys.mock.injectors import InjectionContext, inject
    inject(FaultCategory.DB_POOL_EXHAUSTED, ctx)

这里额外保留 ``inject_db_pool_exhausted``，因为它对**单服务**场景更顺手
（M1 的测试和脚本都在用）：

::

    inject_db_pool_exhausted(store, service, at)

**两者走的是同一份实现**（``injectors.db_pool.inject_db_pool``），
不存在两份逻辑各自演化的问题。
"""

from __future__ import annotations

from datetime import datetime

from fivewhys.mock.changes import ConfigStore
from fivewhys.mock.injectors.db_pool import inject_db_pool
from fivewhys.mock.logstore import LogStore
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.service import MockService
from fivewhys.models import GroundTruth


def inject_db_pool_exhausted(
    store: LogStore,
    service: MockService,
    at: datetime,
    *,
    metrics: MetricStore | None = None,
    configs: ConfigStore | None = None,
) -> GroundTruth:
    """注入「数据库连接池耗尽」故障（单服务便捷入口）。

    Args:
        store: 日志仓库。``service`` 内部已经持有它，所以这个参数其实用不到；
            保留是为了向后兼容（M1 的调用方都这么传）。新代码请直接用
            ``inject(FaultCategory.DB_POOL_EXHAUSTED, ctx)``。
        service: 被注入故障的服务
        at: 故障开始的时间点
        metrics: 指标仓库。传了的话，故障会**同时**反映到指标上。
        configs: 配置历史仓库。传了的话，会记下**真正的根因** ——
            ``db.pool_size`` 从 50 变成 5。

    Returns:
        这个场景的 ground truth，供评测判分使用。
    """
    return inject_db_pool(
        service=service,
        at=at,
        rng=service.rng,
        metrics=metrics,
        configs=configs,
    )


__all__ = ["inject_db_pool_exhausted"]
