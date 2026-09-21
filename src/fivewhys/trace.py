"""轨迹落盘 —— 把一次诊断的**每一步**写下来（需求 FR-9）。

## 为什么它比看上去重要

上线前审查里有一条至今没查明的失败：

    第一版 M1 验收是「100%（5/5）」，后来又跑了几批，
    其中一批 2 次里有 1 次没到 80 分 —— **那次的轨迹没留下来，
    所以不知道它错在哪。**

于是 README 里的数字从「100%」改成了「93%（14/15）」，而根因仍然是个谜。
这个模块就是为了让下一次失败**有东西可查**。

ROADMAP 里写得很直白：**「trace 是 M6/M7 分析失败、产出指标的唯一依据」**。
没有它，M6 只能算出一个总准确率，说不出「为什么错」；M7 的改进也就无从定位。

## 格式：JSONL，一行一个事件，**边跑边写**

::

    runs/<run_id>/trace.jsonl

为什么是 JSONL 而不是一个大 JSON：

1. **崩溃安全** —— 每一步都 flush。跑到第 7 步崩了，前 7 步的轨迹仍然在盘上。
   写成一个 JSON 的话，中途崩溃等于什么都没留下（而那正是最需要轨迹的时候）。
2. **可流式读** —— M6/M7 可以逐行消费，不用把几百 MB 一次性读进内存。
3. **可 diff** —— 两次运行的差异可以直接用文本工具比。

## 事件类型

=================  ====================================================
``start``          这次诊断的输入：问题、模型、可用工具、seed/配置
``request``        发给模型的完整 messages（每步一份，便于复盘）
``response``       模型返回：content / tool_calls / token / 成本 / 耗时
``tool_result``    工具执行结果（含失败的，失败也是一等公民）
``finish``         停止原因、最终结论、总成本/总 token/总耗时、错误
=================  ====================================================

## 两个刻意的设计

- **每步都存完整 messages**，而不是只存增量。体积换可读性：复盘时不需要
  自己把增量拼起来（拼错了就得出错误结论）。实测一次 **5 步**诊断的 trace 约
  **150KB**（因为每步都带完整历史）—— 100 次评测约 15MB。
  与「查不出原因」相比，这点磁盘不算什么：`runs/` 已在 .gitignore 里。
- **工具返回原样存**，不截断。工具返回是 agent 唯一的信息来源，
  截断它就等于把「模型看到了什么」改掉，复盘会失真。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fivewhys.models import AgentRun, Diagnosis

# 轨迹默认落在这里。**已加进 .gitignore** —— 它是运行时数据，不是源码。
DEFAULT_TRACE_ROOT = Path("runs")

EVENT_START = "start"
EVENT_REQUEST = "request"
EVENT_RESPONSE = "response"
EVENT_TOOL_RESULT = "tool_result"
EVENT_FINISH = "finish"


def new_run_id(scenario_id: str, *, at: datetime | None = None) -> str:
    """给一次运行起个可排序、又不会撞的名字。

    ``<scenario_id>-<UTC 时间戳>-<4 位随机>``

    为什么要随机后缀：M6 会**并发**跑评测（同一场景同一秒可能有多次运行），
    只靠时间戳会让两次运行写进同一个目录、互相覆盖 —— 而「覆盖掉的那次」
    正好可能是失败的那次。这个坑不值得冒。
    """
    moment = at or datetime.now(UTC)
    suffix = uuid.uuid4().hex[:4]
    return f"{scenario_id}-{moment:%Y%m%dT%H%M%S}-{suffix}"


class TraceWriter:
    """把一次诊断的过程写成 JSONL。

    用法由主循环负责（见 :func:`fivewhys.agent.loop.diagnose`），
    调用方只管拿 ``path``。
    """

    def __init__(self, run_id: str, root: Path | None = None) -> None:
        self.run_id = run_id
        self.root = root or DEFAULT_TRACE_ROOT
        self.directory = self.root / run_id
        self.path = self.directory / "trace.jsonl"
        self._started = False

    # ---- 写入 ----

    def _append(self, kind: str, payload: dict[str, Any]) -> None:
        """追加一个事件。

        ``kind`` 放在每行的最前面，``ts`` 紧随其后 —— 用肉眼扫一遍就知道
        「第几步、什么事」，不必解析整行。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {"kind": kind, "ts": datetime.now(UTC).isoformat(), **payload}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def start(
        self,
        *,
        scenario_id: str,
        question: str,
        model: str,
        tool_names: list[str],
        settings: dict[str, Any] | None = None,
    ) -> None:
        if self._started:  # pragma: no cover —— 主循环只会调一次
            return
        self._started = True
        self._append(
            EVENT_START,
            {
                "run_id": self.run_id,
                "scenario_id": scenario_id,
                "question": question,
                "model": model,
                "tools": tool_names,
                # 配置也记下来：C-7 要求所有数字都能说清「用什么跑的」
                "settings": settings or {},
            },
        )

    def request(self, *, step: int, messages: list[dict[str, Any]]) -> None:
        """这一轮发给模型的完整 messages。"""
        self._append(EVENT_REQUEST, {"step": step, "messages": messages})

    def response(
        self,
        *,
        step: int,
        content: str | None,
        tool_calls: list[dict[str, Any]],
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        latency_ms: int,
    ) -> None:
        self._append(
            EVENT_RESPONSE,
            {
                "step": step,
                "content": content,
                "tool_calls": tool_calls,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
            },
        )

    def tool_result(
        self,
        *,
        step: int,
        tool: str,
        args: dict[str, Any],
        ok: bool,
        result: str,
        error: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        # 失败的调用也照样记 —— 「模型试了什么、错在哪」和成功的同样重要
        self._append(
            EVENT_TOOL_RESULT,
            {
                "step": step,
                "tool": tool,
                "args": args,
                "ok": ok,
                "result": result,
                "error": error,
                "latency_ms": latency_ms,
            },
        )

    def finish(self, run: AgentRun, *, diagnosis: Diagnosis | None = None) -> None:
        """收尾事件：停止原因 + 结论 + 总量。

        **必须放在 finally 里调** —— 异常路径也要有 finish，
        否则复盘时无法区分「跑完了」和「进程被杀了」。
        """
        # 先取出来再判断：写成 (a or b).model_dump() if (a or b) else None
        # mypy 收窄不了，会报 union-attr（实测）。
        final = diagnosis or run.diagnosis
        self._append(
            EVENT_FINISH,
            {
                "stop_reason": run.stop_reason,
                "error": run.error,
                "steps": run.steps,
                "tool_calls": len(run.tool_calls),
                "total_cost_usd": run.total_cost_usd,
                "total_tokens": run.total_tokens,
                "duration_s": run.duration_s,
                "diagnosis": final.model_dump(mode="json") if final else None,
            },
        )


# --------------------------------------------------------------------------
# 读回来（给人看，也给 M6/M7 消费）
# --------------------------------------------------------------------------


def read_events(path: Path) -> list[dict[str, Any]]:
    """把一个 trace.jsonl 读成事件列表。

    坏行不抛异常：轨迹是**诊断产物**，读它的时候最不希望的就是
    「因为最后一行写了一半而整个读不出来」。坏行原样保留成 ``{"kind": "broken"}``，
    这样复盘时能看出「这里写到一半断了」——那本身就是线索。
    """
    events: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"kind": "broken", "line": number, "raw": line[:200]})
    return events


def find_run(run_ref: str, root: Path | None = None) -> Path:
    """按 run_id 找轨迹；``latest`` / ``last`` 取最近一次。"""
    base = root or DEFAULT_TRACE_ROOT
    if run_ref in {"latest", "last"}:
        candidates = sorted(
            (path for path in base.glob("*/trace.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        if not candidates:
            raise FileNotFoundError(f"{base} 下还没有任何轨迹。先跑一次诊断。")
        return candidates[-1]

    direct = base / run_ref / "trace.jsonl"
    if direct.is_file():
        return direct
    # 允许只给前缀（时间戳太长，没人愿意敲全）
    matches = sorted(path for path in base.glob(f"{run_ref}*/trace.jsonl") if path.is_file())
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"没找到轨迹「{run_ref}」（在 {base} 下）")
    raise FileNotFoundError(
        f"「{run_ref}」匹配到 {len(matches)} 个轨迹，请写全："
        + "、".join(p.parent.name for p in matches)
    )


def summarize(path: Path) -> dict[str, Any]:
    """把一条轨迹压成「一眼能看懂」的摘要（给 CLI 用）。

    刻意只统计**发生了什么**，不做任何解释 —— 解释是人的事，
    工具歪曲事实比不给工具更糟。
    """
    events = read_events(path)
    start = next((e for e in events if e["kind"] == EVENT_START), {})
    finish = next((e for e in reversed(events) if e["kind"] == EVENT_FINISH), {})
    responses = [e for e in events if e["kind"] == EVENT_RESPONSE]
    tools = [e for e in events if e["kind"] == EVENT_TOOL_RESULT]

    return {
        "run_id": start.get("run_id") or path.parent.name,
        "scenario_id": start.get("scenario_id"),
        "question": start.get("question"),
        "model": start.get("model"),
        "steps": len(responses),
        "tool_calls": len(tools),
        "failed_tool_calls": sum(1 for t in tools if not t.get("ok")),
        "stop_reason": finish.get("stop_reason"),
        "error": finish.get("error"),
        "total_cost_usd": finish.get("total_cost_usd"),
        "total_tokens": finish.get("total_tokens"),
        "duration_s": finish.get("duration_s"),
        "diagnosis": finish.get("diagnosis"),
        "events": len(events),
        "broken_lines": sum(1 for e in events if e["kind"] == "broken"),
        "has_finish": bool(finish),
    }


__all__ = [
    "DEFAULT_TRACE_ROOT",
    "EVENT_FINISH",
    "EVENT_REQUEST",
    "EVENT_RESPONSE",
    "EVENT_START",
    "EVENT_TOOL_RESULT",
    "TraceWriter",
    "find_run",
    "new_run_id",
    "read_events",
    "summarize",
]
