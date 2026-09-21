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

from fivewhys.mock.logstore import LogStore
from fivewhys.mock.metrics import MetricStore
from fivewhys.mock.scenarios import inject_db_pool_exhausted
from fivewhys.mock.topology import MockSystem
from fivewhys.models import GroundTruth

MANIFEST_NAME = "scenario.json"
LOGS_NAME = "logs.jsonl"
METRICS_NAME = "metrics.jsonl"

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
        return target

    @classmethod
    def load(cls, path: Path) -> Scenario:
        """从 ``<root>/<scenario_id>/`` 加载。"""
        manifest_path = path / MANIFEST_NAME
        if not manifest_path.exists():
            raise FileNotFoundError(f"场景包里没有 {MANIFEST_NAME}：{path}")

        manifest = ScenarioManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        return cls(
            manifest=manifest,
            logs=LogStore.load_jsonl(path / LOGS_NAME),
            metrics=MetricStore.load_jsonl(path / METRICS_NAME),
        )

    def summary(self) -> str:
        """一行摘要，给人看的。"""
        return (
            f"{self.scenario_id}  日志 {len(self.logs)} 条  "
            f"指标 {len(self.metrics)} 条  服务 {len(self.topology)} 个"
        )


# --------------------------------------------------------------------------
# 构造器
# --------------------------------------------------------------------------


def build_db_pool_scenario(
    *,
    seed: int = 0,
    base_time: datetime | None = None,
    warmup_minutes: int = 2,
    rps: float = 2.0,
) -> Scenario:
    """构造一个「连接池耗尽」场景并打包。

    ``question`` 的措辞遵循需求 FR-15：**给服务名和粗略时间，不给根因**。

        提供                 不提供
        ─────────────────    ──────────────────
        ✅ 服务名             ❌ 具体错误信息
        ✅ 粗略时间（分钟）    ❌ 精确时间窗口
        ✅ 表面症状           ❌ 根因 / 故障类别
    """
    base = base_time or datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    fault_at = base + timedelta(minutes=warmup_minutes)

    logs = LogStore()
    metrics = MetricStore()
    system = MockSystem(logs, metrics=metrics, seed=seed)
    system.normal_operation(base, fault_at, rps=rps)

    truth = inject_db_pool_exhausted(
        logs,
        system.service("order-service"),
        fault_at,
        metrics=metrics,
    )

    question = f"order-service 从 {fault_at:%H:%M} 前后开始错误率飙升，帮忙定位一下原因"

    return Scenario(
        manifest=ScenarioManifest(
            scenario_id=truth.scenario_id,
            question=question,
            ground_truth=truth,
            topology=system.describe(),
        ),
        logs=logs,
        metrics=metrics,
    )


__all__ = [
    "DEFAULT_SCENARIO_ROOT",
    "LOGS_NAME",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "METRICS_NAME",
    "Scenario",
    "ScenarioManifest",
    "build_db_pool_scenario",
]
