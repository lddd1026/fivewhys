"""一个本地假 LLM 服务 —— 让端到端链路在**不花钱、不联网**的前提下跑通。

## 它验证什么

把 ``FIVWHYS_API_BASE`` 指向这个服务，``demo_m1.py`` 就会走完整条真实路径：

    子进程启动 -> get_settings 读环境变量 -> LiteLLMClient 构造
      -> litellm 发真实 HTTP 请求 -> 这个服务返回响应
      -> LiteLLMClient 解析 -> 工具分发 -> query_logs 真的被调用
      -> 提交结论 -> 判分 -> 输出表格与判定

**除了「模型智力」以外，全部是真的。** 这比用假 Python 对象（ScriptedLLM）
更进一步 —— 它能抓到 HTTP 层、序列化、litellm 适配层的问题。

## 它不验证什么

模型的真实能力。这里的响应是脚本化的，不会推理。
所以它**不能替代 FIV-5 的真实运行**，只能证明「链路是通的」。

## 为什么用标准库而不是 FastAPI

``http.server`` 就够，而且不引入任何依赖（NFR-6）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Messages = list[dict[str, Any]]
Responder = Callable[[Messages], dict[str, Any]]


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    """构造一个 OpenAI 形状的「模型要求调用工具」响应。"""
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek/deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
    }


def _called_tools(messages: Messages) -> list[str]:
    names: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                names.append(call.get("function", {}).get("name", ""))
    return names


def ideal_responder(messages: Messages) -> dict[str, Any]:
    """一个「完美模型」：**按真实排障路径走一遍 5 个工具**，再提交正确结论。

    它走过的是：

    ::

        query_metrics    错误率从 14:02 起飙升（拿到时间点）
          -> query_logs    deadline exceeded + connection wait time 飙升
            -> get_config  db.pool_size: 50 -> 5      <- 根因
              -> get_deploy_history  窗口内没有发布（排除嫌疑）
                -> submit_diagnosis

    为什么脚本要绕这么一圈，而不是直接提交答案：这样端到端测试才真的
    穿过**主循环的工具分发 + 完整的证据链**。以前它只调 query_logs，
    另外四个工具在端到端里从来没被调用过 —— 链路上任何一处坏了都测不出来。

    只针对 ``db_pool_exhausted`` 这一个场景 —— 这就是个脚本，不做推理。
    """
    called = _called_tools(messages)
    service = "order-service"
    # 时间窗口故意开得很宽，覆盖整个场景
    window = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}

    if "query_metrics" not in called:
        return _tool_call("query_metrics", {"service": service, **window}, "call_metrics")

    if "query_logs" not in called:
        return _tool_call("query_logs", {"service": service, **window}, "call_logs")

    if "get_config" not in called:
        return _tool_call("get_config", {"service": service}, "call_config")

    if "get_deploy_history" not in called:
        return _tool_call("get_deploy_history", {"service": service, **window}, "call_deploys")

    return _tool_call(
        "submit_diagnosis",
        {
            "root_cause": "order-service 的数据库连接池上限被配置变更下调（50 -> 5），导致连接耗尽",
            "root_cause_service": "order-service",
            "fault_category": "db_pool_exhausted",
            "confidence": "high",
            "why_chain": [
                {
                    "depth": 1,
                    "question": "为什么错误率飙升？",
                    "answer": "order lookup 大量 context deadline exceeded，P95 冲到 3s",
                    "evidence": [
                        {
                            "source": "query_metrics(order-service)",
                            "finding": "错误率从 14:02 起从 0% 升到 13%，P95 从 102ms 升到 3055ms",
                            "supports": True,
                        }
                    ],
                },
                {
                    "depth": 2,
                    "question": "为什么请求会超时？",
                    "answer": "请求在等待数据库连接，connection wait time 涨到 3000ms",
                    "evidence": [
                        {
                            "source": "query_logs(order-service)",
                            "finding": "connection wait time 3023ms exceeds threshold 100ms",
                            "supports": True,
                        }
                    ],
                },
                {
                    "depth": 3,
                    "question": "为什么连接要排队等？",
                    "answer": "连接池上限被从 50 改成了 5，并发请求只能排队",
                    "evidence": [
                        {
                            "source": "get_config(order-service)",
                            "finding": "14:01:58 db.pool_size: 50 -> 5",
                            "supports": True,
                        }
                    ],
                },
            ],
            "evidence": [
                {
                    "source": "get_config(order-service)",
                    "finding": "配置变更 db.pool_size: 50 -> 5 发生在错误开始前 2 秒",
                    "supports": True,
                }
            ],
            "ruled_out": [
                "发布引起的：get_deploy_history 显示故障窗口内没有发布",
                "下游服务：它们在同一时间窗口内没有异常",
            ],
            "suggested_fix": "回滚配置变更，恢复连接池上限到 50",
            "summary": "连接池上限被改成 5 导致连接耗尽，order-service 大面积超时",
        },
        "call_submit",
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 —— 标准库要求这个名字
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
            messages = body.get("messages", [])
            responder: Responder = self.server.responder  # type: ignore[attr-defined]
            payload = json.dumps(responder(messages), ensure_ascii=False).encode("utf-8")
            status = 200
        except Exception as exc:  # noqa: BLE001 —— 假服务出错也要让调用方看到原因
            payload = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        """静音访问日志，否则测试输出会被刷屏。"""


@contextmanager
def fake_llm_server(responder: Responder = ideal_responder) -> Iterator[str]:
    """启动假服务，产出它的 base_url。退出时自动关闭。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.responder = responder  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


__all__ = ["fake_llm_server", "ideal_responder"]
