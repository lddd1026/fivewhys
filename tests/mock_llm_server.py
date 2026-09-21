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
    """一个「完美模型」：先查日志，再提交正确结论。

    只针对 ``db_pool_exhausted`` 这一个场景 —— 这就是个脚本，不做推理。
    """
    called = _called_tools(messages)

    if "query_logs" not in called:
        return _tool_call(
            "query_logs",
            # 时间窗口故意开得很宽，覆盖整个场景
            {
                "service": "order-service",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-02T00:00:00Z",
            },
            "call_query",
        )

    return _tool_call(
        "submit_diagnosis",
        {
            "root_cause": "order-service 的数据库连接池上限被配置变更下调，导致连接耗尽",
            "root_cause_service": "order-service",
            "fault_category": "db_pool_exhausted",
            "confidence": "high",
            "why_chain": [
                {
                    "depth": 1,
                    "question": "为什么错误率飙升？",
                    "answer": "order lookup 大量 context deadline exceeded",
                    "evidence": [],
                },
                {
                    "depth": 2,
                    "question": "为什么请求会超时？",
                    "answer": "请求在等待数据库连接，connection wait time 涨到 3000ms",
                    "evidence": [],
                },
            ],
            "evidence": [
                {
                    "source": "query_logs(order-service)",
                    "finding": "connection wait time 从 0ms 飙升到约 3000ms",
                    "supports": True,
                }
            ],
            "ruled_out": ["下游服务在该时间窗口内没有异常日志"],
            "suggested_fix": "回滚配置变更，恢复连接池上限",
            "summary": "连接池耗尽导致 order-service 大面积超时",
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
