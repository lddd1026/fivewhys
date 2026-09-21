"""FIV-14 手工检查：把 5 条工具说明书当一篇文章读一遍。

用法::

    python scripts/review_tool_descriptions.py
    python scripts/review_tool_descriptions.py --budget   # 只看预算汇总

不调 LLM、不联网。它把模型**实际会读到的东西**原样打出来：
工具描述 + 每个参数的描述。读完你就能判断：

- 模型光看这些字，知不知道**第一步该用哪个工具**
- 有没有工具没写清「什么时候不该用它」
- 描述总量有多少 —— 提示词每次请求都要发，这些字是**每次都花钱**的
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

from fivewhys.mock.logstore import LogStore  # noqa: E402
from fivewhys.tools import TOOL_DESCRIPTION_BUDGET_CHARS, DataSource, build_registry  # noqa: E402

console = Console()

# 预算定义在工具层（tools/__init__.py），因为它是 NFR-2 成本红线的一部分，
# 不是这个脚本的私有约定。
BUDGET_CHARS = TOOL_DESCRIPTION_BUDGET_CHARS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通读工具说明书")
    parser.add_argument("--budget", action="store_true", help="只打印预算汇总")
    args = parser.parse_args(argv)

    registry = build_registry(DataSource.logs_only(LogStore()))
    description_chars = 0
    parameter_chars = 0

    for spec in registry.specs():
        function = spec["function"]
        description = function["description"]
        description_chars += len(description)

        # ⚠️ 参数长度必须**无条件**累加：只在打印分支里统计的话，
        # --budget 模式会永远显示「参数 0 字」—— 预算看起来凭空充裕了一截。
        # （这个 bug 是写完脚本当场跑 --budget 时发现的。）
        required = set(function["parameters"].get("required", []))
        for prop in function["parameters"]["properties"].values():
            parameter_chars += len(prop.get("description", ""))

        if not args.budget:
            console.print(
                Panel(
                    description,
                    title=f"{function['name']} · 描述 {len(description)} 字",
                    title_align="left",
                )
            )
            for name, prop in function["parameters"]["properties"].items():
                param_desc = prop.get("description", "")
                mark = "必填" if name in required else "选填"
                console.print(
                    f"  [cyan]{name}[/cyan] [dim]({mark} {prop.get('type')})[/dim] {param_desc}"
                )
            console.print()

    total = description_chars + parameter_chars
    color = "green" if total <= BUDGET_CHARS else "red"
    console.print(
        f"描述 {description_chars} 字 + 参数 {parameter_chars} 字 = "
        f"[{color}]{total} 字[/{color}]（预算 {BUDGET_CHARS}）"
        f"，约 {total // 3} token / 次请求"
    )
    return 0 if total <= BUDGET_CHARS else 1


if __name__ == "__main__":
    raise SystemExit(main())
