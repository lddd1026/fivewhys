"""工具层。

**设计要点（面试一定会问）**：工具粒度怎么定？

常见错误做法有两种：
  - 太粗：给一个万能 `query(anything)`，模型不知道该查什么，容易瞎试
  - 太细：把每一步都做成工具，模型被淹没在选项里

fivewhys 这一版**按数据源切分**（日志 / 指标 / 配置 / 发布 / 拓扑），
因为数据源是客观存在的边界，语义最直白。

> **重要**：这只是一种**假设**，不是结论。
> 更贴合 SRE 真实排障路径的切法（例如 `check_recent_changes` 同时看发布与配置、
> `trace_request` 追一次请求的完整链路）读起来更聪明，但**也可能让模型更困惑**
> —— 它不知道 `check_recent_changes` 到底会查什么。
>
> M7 用一次 A/B 实验对比这两版（需求 §10.3）：
> 这一版是**对照组 A**，也是 M1~M4 的实现。结论无论正反都有价值。
> 详见 [REQUIREMENTS.md §FR-5](../../../docs/REQUIREMENTS.md)。

本模块只提供基础设施（Tool / ToolRegistry / DataSource），具体工具在各自文件里实现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    # 只在类型检查时导入，运行时不导入 —— 否则
    # tools -> mock.logstore -> ... 会形成循环
    from fivewhys.mock.changes import ConfigStore, DeployStore
    from fivewhys.mock.logstore import LogStore
    from fivewhys.mock.metrics import MetricStore
    from fivewhys.scenario import Scenario

# 工具说明书（描述 + 参数描述）的字符总量上限。
#
# 为什么这是工具层的事，而不是文档的事：说明书写在**每一次**请求的提示词里。
# 一个 30 步的诊断会把它原样发 30 遍 —— 多写 1000 字，一次诊断就多花约 1 万 token。
# 成本红线（NFR-2）最终就落在这些字上。
#
# 当前实际约 2100 字，留了余量给「再加一两个工具」。真到顶了，
# 该做的是删废话，而不是调高这个数 —— 调高等于把成本问题挪到看不见的地方。
# 用 `python scripts/review_tool_descriptions.py` 看当前用量。
TOOL_DESCRIPTION_BUDGET_CHARS = 3000


@dataclass(frozen=True)
class DataSource:
    """一个场景**能被工具查到的全部数据**。

    为什么用显式的数据包，而不是给 ``build_registry`` 传五个参数：
    工具集的形状（有哪些工具）取决于有哪些数据源。把这件事说成一个对象，
    「这个场景能给 agent 看什么」就是一个可传递、可断言的东西 ——
    M6 的评测台会把它和 ``Scenario`` 一起搬来搬去，
    M7 的变体 B 也要从同一个包组装另一套工具。

    四个仓库**可以为空**（例如 M1 只有日志的最小场景）：
    那时对应的工具会如实回答「本场景没有指标数据」。这比让工具报错、
    或者干脆不注册它（工具集随场景变化，提示词就没法稳定）要好。
    """

    logs: LogStore
    metrics: MetricStore
    configs: ConfigStore
    deploys: DeployStore
    topology: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_scenario(cls, scenario: Scenario) -> DataSource:
        """从落盘的场景包构造 —— 这是 M6 评测台的入口。"""
        return cls(
            logs=scenario.logs,
            metrics=scenario.metrics,
            configs=scenario.configs,
            deploys=scenario.deploys,
            topology=dict(scenario.topology),
        )

    @classmethod
    def logs_only(cls, store: LogStore) -> DataSource:
        """只有日志的最小场景（M1 的形态）。

        其余四个数据源留空 —— 对应的工具会**如实回答**「本场景没有这类数据」，
        而不是报错或假装有数据。函数内导入是为了避开模块级循环依赖
        （和 :func:`build_registry` 里同样的理由）。
        """
        from fivewhys.mock.changes import ConfigStore, DeployStore
        from fivewhys.mock.metrics import MetricStore

        return cls(
            logs=store,
            metrics=MetricStore(),
            configs=ConfigStore(),
            deploys=DeployStore(),
        )

    @property
    def services(self) -> list[str]:
        """这个场景里**存在**的服务名，用来纠正模型写错的服务名。

        优先相信拓扑 —— 它是场景的定义，不依赖某个数据源恰好有数据。
        没有拓扑（M1 的单服务最小场景）时，退回「各数据源里出现过的服务」的并集。
        """
        if self.topology:
            return sorted(self.topology)
        names = set(self.metrics.services())
        names.update(entry.service for entry in self.logs.all())
        names.update(self.configs.services())
        names.update(record.service for record in self.deploys.all())
        return sorted(names)


@dataclass(frozen=True)
class Tool:
    """一个可供 agent 调用的工具。

    参数用 Pydantic 模型声明，JSON Schema 自动生成 —— 不手写 schema，
    这样参数校验和给模型看的说明书永远是一致的。
    """

    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[..., Any]

    def to_openai_spec(self) -> dict[str, Any]:
        """转成 OpenAI function-calling 格式。litellm 会把各家的差异抹平。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }

    def __call__(self, **kwargs: Any) -> Any:
        """调用工具。参数先过一遍校验，避免把脏参数传进业务逻辑。"""
        validated = self.args_model(**kwargs)
        return self.func(**validated.model_dump())


@dataclass
class ToolRegistry:
    """工具注册表。agent 主循环只跟它打交道。"""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未知工具「{name}」。可用：{sorted(self._tools)}")
        return self._tools[name]

    def specs(self) -> list[dict[str, Any]]:
        """给模型看的工具清单。"""
        return [tool.to_openai_spec() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def build_registry(source: DataSource) -> ToolRegistry:
    """组装 agent 可用的全部工具。

    这是「工具集合的唯一入口」：改工具组合只改这一个地方，
    agent 主循环、评测台、CLI 都从这里拿。

    工具的**顺序**也是有意的：按一次真实排障的先后排。
    提示词里给模型看的就是这个顺序（见 :mod:`fivewhys.agent.prompts`）。

    Args:
        source: 场景的数据包。每个场景一个独立的包，
            并发跑评测时不会互相污染。

    Returns:
        装好全部工具的注册表。
    """
    # 延迟导入，避免循环依赖：
    #   tools/__init__ -> tools/query_logs -> tools/__init__（拿 Tool）
    # 在函数内部导入时，本模块的 Tool / ToolRegistry / DataSource 已经定义完了。
    from fivewhys.tools.get_config import build_get_config_tool
    from fivewhys.tools.get_dependencies import build_get_dependencies_tool
    from fivewhys.tools.get_deploy_history import build_get_deploy_history_tool
    from fivewhys.tools.query_logs import build_query_logs_tool
    from fivewhys.tools.query_metrics import build_query_metrics_tool

    services = source.services
    registry = ToolRegistry()

    # 1. 指标：先确认「有没有问题、从什么时候开始」
    registry.register(build_query_metrics_tool(source.metrics, services=services))
    # 2. 日志：异常时间点上的具体报错
    registry.register(build_query_logs_tool(source.logs, services=services))
    # 3. 配置：根因最常藏在这里
    registry.register(build_get_config_tool(source.configs, services=services))
    # 4. 发布：找嫌疑，也用来排除嫌疑
    registry.register(build_get_deploy_history_tool(source.deploys, services=services))
    # 5. 拓扑：症状在上游、根因在下游时顺着调用链追
    registry.register(build_get_dependencies_tool(source.topology))
    return registry


__all__ = [
    "DataSource",
    "TOOL_DESCRIPTION_BUDGET_CHARS",
    "Tool",
    "ToolRegistry",
    "build_registry",
]
