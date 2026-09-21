"""场景包 —— 把一次评测所需的一切打成一个可独立加载的包。

## 需求 FR-15

每个场景由三部分组成，必须**整体落盘、可独立加载、可复现**：

============  ==========================================  ===========
组成         内容                                        agent 可见
============  ==========================================  ===========
``question``  现象描述，agent 收到的初始输入                ✅
``data``      模拟数据（日志 / 指标），agent 通过工具查       ✅
``ground_truth``  标准答案                                  ❌ 不可见
============  ==========================================  ===========

## 为什么要落盘

1. **可复现** —— 评测跑的是文件，不是"当场随机生成的东西"。
   同一份场景包，谁跑、什么时候跑，结果都一样。
2. **可审计** —— 你能打开文件，亲眼看到 agent 拿到了什么、答案是什么。
3. **可对比** —— M7 要比较"改进前 vs 改进后"，前提是**场景完全相同**。
   如果每次现生成，噪声种子一变，曲线就不可信。

## 目录结构

::

    data/scenarios/<scenario_id>/
    ├── scenario.json     # question + ground_truth + topology
    ├── logs.jsonl        # 日志
    └── metrics.jsonl     # 指标

## 为什么 ground_truth 和 question 放在同一个文件里

听起来危险 —— 但 agent 从来不会读这个目录。它是给评测台和**你**看的。
真正要防的是「agent 能从 question 或日志里读出答案」，而那由
:meth:`Scenario.validate` 自动检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from fivewhys.mock.changes import ConfigStore, DeployStore
from fivewhys.mock.injectors import InjectionContext, available, inject
from fivewhys.mock.logstore import LogStore
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.topology import MockSystem
from fivewhys.models import FaultCategory, GroundTruth

MANIFEST_NAME = "scenario.json"
LOGS_NAME = "logs.jsonl"
METRICS_NAME = "metrics.jsonl"
CONFIGS_NAME = "configs.jsonl"
DEPLOYS_NAME = "deploys.jsonl"

# 默认存放位置。已在 .gitignore 里（data/*）。
DEFAULT_SCENARIO_ROOT = Path("data/scenarios")

# 清单格式版本。将来改结构时用它做兼容判断。
MANIFEST_VERSION = 1


class ScenarioManifest(BaseModel):
    """场景的元信息（不含数据本身）。"""

    version: int = MANIFEST_VERSION
    scenario_id: str
    question: str = Field(description="给 agent 看的现象描述")
    ground_truth: GroundTruth
    topology: dict[str, list[str]] = Field(
        default_factory=dict,
        description="服务的调用关系，例如 {'order-service': ['payment-service']}",
    )


@dataclass
class Scenario:
    """一个完整的评测场景：问题 + 数据 + 标准答案。"""

    manifest: ScenarioManifest
    logs: LogStore
    metrics: MetricStore
    configs: ConfigStore
    deploys: DeployStore

    # ---- 便捷访问 ----

    @property
    def scenario_id(self) -> str:
        return self.manifest.scenario_id

    @property
    def question(self) -> str:
        return self.manifest.question

    @property
    def ground_truth(self) -> GroundTruth:
        return self.manifest.ground_truth

    @property
    def topology(self) -> dict[str, list[str]]:
        return self.manifest.topology

    # ---- 校验 ----

    def validate(self) -> list[str]:
        """检查这个场景是否可用。返回问题列表，空列表表示通过。

        这是需求 FR-2 / FR-15 的**可自动执行的判据**：
        把「设计约束」变成「可检查的断言」，而不是靠人记得。
        """
        problems: list[str] = []
        truth = self.ground_truth

        # FR-2：日志里不得出现答案词
        log_text = " ".join(entry.message.lower() for entry in self.logs.all())
        leaked = [kw for kw in truth.answer_keywords if kw.lower() in log_text]
        if leaked:
            problems.append(f"日志里泄漏了答案词：{leaked}")

        # FR-15：question 里不得出现判分词
        question_lower = self.question.lower()
        leaked_q = [kw for kw in truth.match_keywords if kw.lower() in question_lower]
        if leaked_q:
            problems.append(f"question 里泄漏了判分词：{leaked_q}")

        # 数据不能为空
        if len(self.logs) == 0:
            problems.append("日志为空")
        if len(self.metrics) == 0:
            problems.append("指标为空")
        if len(self.configs) == 0:
            problems.append("配置历史为空")

        # 故障场景的配置历史里必须真的有变化 —— 那是根因所在。
        # 如果配置从头到尾没变过，agent 查了也白查，这个场景是坏的。
        if truth.fault_category != "no_fault":
            changes = self.configs.changes(truth.root_cause_service)
            if not changes:
                problems.append(
                    f"故障场景的配置历史里没有任何变化（根因服务 "
                    f"{truth.root_cause_service}）—— 根因证据缺失"
                )

        # 拓扑要覆盖根因服务
        if self.topology and truth.root_cause_service not in self.topology:
            problems.append(f"拓扑里没有根因服务「{truth.root_cause_service}」")

        # 正常场景不该有失败采样
        if truth.fault_category == "no_fault":
            failed = [s for s in self.metrics.all() if s.failed]
            if failed:
                problems.append(f"正常场景不该有失败请求，实际有 {len(failed)} 条")

        return problems

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # ---- 持久化 ----

    def save(self, root: Path | None = None) -> Path:
        """落盘到 ``<root>/<scenario_id>/``。返回目标目录。"""
        target = (root or DEFAULT_SCENARIO_ROOT) / self.scenario_id
        target.mkdir(parents=True, exist_ok=True)

        (target / MANIFEST_NAME).write_text(
            self.manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        self.logs.dump_jsonl(target / LOGS_NAME)
        self.metrics.dump_jsonl(target / METRICS_NAME)
        self.configs.dump_jsonl(target / CONFIGS_NAME)
        self.deploys.dump_jsonl(target / DEPLOYS_NAME)
        return target

    @classmethod
    def load(cls, path: Path) -> Scenario:
        """从 ``<root>/<scenario_id>/`` 加载。"""
        manifest_path = path / MANIFEST_NAME
        if not manifest_path.exists():
            raise FileNotFoundError(f"场景包里没有 {MANIFEST_NAME}：{path}")

        manifest = ScenarioManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))

        def _optional(loader: object, name: str) -> object:
            """配置文件可能来自旧版本场景包，缺了就当空。"""
            target = path / name
            return loader(target) if target.exists() else None  # type: ignore[operator]

        return cls(
            manifest=manifest,
            logs=LogStore.load_jsonl(path / LOGS_NAME),
            metrics=MetricStore.load_jsonl(path / METRICS_NAME),
            configs=_optional(ConfigStore.load_jsonl, CONFIGS_NAME) or ConfigStore(),  # type: ignore[arg-type]
            deploys=_optional(DeployStore.load_jsonl, DEPLOYS_NAME) or DeployStore(),  # type: ignore[arg-type]
        )

    def summary(self) -> str:
        """一行摘要，给人看的。"""
        return (
            f"{self.scenario_id}  日志 {len(self.logs)} 条  指标 {len(self.metrics)} 条  "
            f"配置快照 {len(self.configs)} 条  发布 {len(self.deploys)} 条  "
            f"服务 {len(self.topology)} 个"
        )


# --------------------------------------------------------------------------
# 构造器
# --------------------------------------------------------------------------


def build_question(category: FaultCategory, target: str, at: datetime) -> str:
    """按需求 FR-15 生成题面：**给服务名和粗略时间，不给根因**。

    ================  ==================================================
    提供              不提供
    ================  ==================================================
    ✅ 服务名          ❌ 具体错误信息
    ✅ 粗略时间（分钟） ❌ 精确时间窗口
    ✅ 表面症状        ❌ 根因 / 故障类别
    ================  ==================================================

    ⚠️ 题面里写的是**被问的服务**（``target``），不是 ``root_cause_service``。

    对 ``dependency_5xx`` 这类场景，两者**不是同一个**：题面问 order-service，
    但根因在 inventory-service。写根因就等于把答案写在题面上。
    """
    if category is FaultCategory.NO_FAULT:
        return f"有用户反馈 {target} 在 {at:%H:%M} 前后偶尔变慢，帮忙确认一下是不是真的有问题"
    return f"{target} 从 {at:%H:%M} 前后开始错误率飙升，帮忙定位一下原因"


def build_scenario(
    category: FaultCategory = FaultCategory.DB_POOL_EXHAUSTED,
    *,
    seed: int = 0,
    base_time: datetime | None = None,
    warmup_minutes: int = 2,
    rps: float = 2.0,
    target: str | None = None,
) -> Scenario:
    """构造任意类别的场景并打包。

    Args:
        category: 故障类别。见 ``fivewhys.mock.catalogue()``。
        seed: 随机种子。同一个种子产出完全相同的场景（NFR-1）。
        base_time: 时间线的起点。
        warmup_minutes: 故障前先跑多久的正常流量。
            **不能是 0** —— 没有正常基线，「什么时候开始坏的」就无从判断。
        rps: 故障前的请求速率。
        target: 题面里问哪个服务。默认入口服务（order-service）。

    Returns:
        打包好的场景。
    """
    base = base_time or datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    fault_at = base + timedelta(minutes=warmup_minutes)
    asked_about = target or "order-service"

    logs = LogStore()
    system = MockSystem(logs, seed=seed)
    system.normal_operation(base, fault_at, rps=rps)

    truth = inject(
        category,
        InjectionContext(
            system=system,
            at=fault_at,
            target=asked_about,
            seed=seed,
        ),
    )

    return Scenario(
        manifest=ScenarioManifest(
            scenario_id=truth.scenario_id,
            question=build_question(category, asked_about, fault_at),
            ground_truth=truth,
            topology=system.describe(),
        ),
        logs=logs,
        metrics=system.metrics,
        configs=system.configs,
        deploys=system.deploys,
    )


def build_db_pool_scenario(
    *,
    seed: int = 0,
    base_time: datetime | None = None,
    warmup_minutes: int = 2,
    rps: float = 2.0,
) -> Scenario:
    """构造一个「连接池耗尽」场景。:func:`build_scenario` 的便捷写法。"""
    return build_scenario(
        FaultCategory.DB_POOL_EXHAUSTED,
        seed=seed,
        base_time=base_time,
        warmup_minutes=warmup_minutes,
        rps=rps,
    )


def build_all_scenarios(
    *,
    seed: int = 0,
    base_time: datetime | None = None,
) -> list[Scenario]:
    """把注册表里的**每一种**故障各构造一个场景。

    M6 的评测台会用它铺开评测集。
    """
    return [build_scenario(category, seed=seed, base_time=base_time) for category in available()]


__all__ = [
    "CONFIGS_NAME",
    "DEFAULT_SCENARIO_ROOT",
    "DEPLOYS_NAME",
    "LOGS_NAME",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "METRICS_NAME",
    "Scenario",
    "ScenarioManifest",
    "build_all_scenarios",
    "build_db_pool_scenario",
    "build_question",
    "build_scenario",
]
