"""工具层。

**设计要点（面试一定会问）**：工具粒度怎么定？

常见错误做法有两种：
  - 太粗：给一个万能 `query(anything)`，模型不知道该查什么，容易瞎试
  - 太细：按数据源切分（一个工具查日志、一个查指标、一个查配置…），
          模型被迫自己去想「该查哪一类数据」，步数爆炸

fivewhys 的原则：**按 SRE 的真实排障路径切分工具**，让 agent 能模仿专家的工作流。
这也是 M7「工具粒度优化」那一轮要验证的假设。

本模块只提供基础设施（Tool / ToolRegistry），具体工具在各自文件里实现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


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


__all__ = ["Tool", "ToolRegistry"]
