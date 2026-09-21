"""上线前加固的回归测试：失败必须有界、有话说、不泄密。

对应上线审查（docs/RELEASE_REVIEW.md）里的 PRE-1 / PRE-2。

## 为什么单独一个文件

这些不是「功能对不对」的测试，而是「**坏的时候是什么样**」的测试。
上线前实测发现三个问题，都属于这一类：

1. provider 挂死（连上了、不回包）→ 调用无限等（litellm 默认 600s × 20 步）
2. 坏 API Key → 结果表格之前甩 60 行 litellm 内部 traceback
3. 表格里只写「未提交结论（error）」—— 不说 401 还是网络断

功能测试永远测不到这三条：happy path 上它们都不发生。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fivewhys.agent.llm import LiteLLMClient
from fivewhys.config import Settings
from fivewhys.logs import configure_logging

# --------------------------------------------------------------------------
# 一个「敌意 provider」：想回什么就回什么，包括什么都不回
# --------------------------------------------------------------------------


class _HostileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "hang"

    def do_POST(self) -> None:  # noqa: N802 —— 标准库要求这个名字
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if self.mode == "hang":
            time.sleep(60)  # 收了请求，永不回应
            return

        if self.mode == "garbage":
            self._send(200, b"<html>502 Bad Gateway</html>")
        elif self.mode == "http500":
            self._send(500, json.dumps({"error": {"message": "boom"}}).encode())
        elif self.mode == "rate_limit":
            self._send(429, json.dumps({"error": {"message": "rate limit"}}).encode())
        elif self.mode == "no_choices":
            self._send(200, json.dumps({"id": "x", "choices": []}).encode())

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        """静音访问日志。"""


@pytest.fixture
def hostile_server():
    """起一个敌意 provider，产出 ``(base_url, set_mode)``。"""
    servers: list[ThreadingHTTPServer] = []

    def start(mode: str) -> str:
        _HostileHandler.mode = mode
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostileHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    """一个确定没人监听的端口 —— 用来模拟「连不上」。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# ⭐ PRE-1：调用必须有超时
# --------------------------------------------------------------------------


async def test_call_times_out_instead_of_hanging_forever(hostile_server) -> None:
    """⭐ provider 挂死时，必须在**我们给的超时**内返回，而不是一直等。

    上线前实测：默认配置下 20 秒还没返回 —— litellm 自己的默认超时是 600 秒，
    乘以 max_steps=20 就是最多 3 小时。用户只会以为程序卡了。
    """
    client = LiteLLMClient(
        model="deepseek/deepseek-chat",
        api_base=hostile_server("hang"),
        timeout_s=1.0,
    )

    started = time.perf_counter()
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 —— 类型随 litellm 版本变
        await asyncio.wait_for(
            client.complete([{"role": "user", "content": "hi"}], []),
            timeout=10.0,  # 兜底：万一将来超时失效，不要让测试自己也挂死
        )
    elapsed = time.perf_counter() - started

    assert elapsed < 8.0, f"等了 {elapsed:.1f}s —— 超时没有生效"
    assert "timeout" in str(excinfo.value).lower() or "timed out" in str(excinfo.value).lower()


def test_timeout_is_passed_through_to_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时必须真的传给 litellm，而不是只存在我们的字段里。"""
    import litellm

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    client = LiteLLMClient(model="m", timeout_s=12.5)

    with pytest.raises(RuntimeError):
        asyncio.run(client.complete([{"role": "user", "content": "hi"}], []))

    assert captured.get("timeout") == 12.5


def test_settings_expose_a_default_timeout() -> None:
    """默认配置里就必须有超时 —— 不能指望用户自己去配。"""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_timeout_s > 0
    assert settings.llm_timeout_s <= 300, "超过 5 分钟的超时等于没有超时"


def test_timeout_is_configurable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIVEWHYS_LLM_TIMEOUT_S", "7.5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_timeout_s == 7.5


# --------------------------------------------------------------------------
# ⭐ PRE-1b：provider 的各种坏法都要变成「有界的、可读的错误」
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["garbage", "http500", "rate_limit", "no_choices"])
async def test_broken_provider_raises_a_real_error(hostile_server, mode: str) -> None:
    """坏响应要抛异常（由主循环记成 error），不能静默返回空结果。

    静默返回才是最危险的：主循环会以为模型没说话，然后一直推它，
    把整个步数预算烧光，最后报「max_steps」—— 用户完全看不出是 provider 坏了。
    """
    client = LiteLLMClient(
        model="deepseek/deepseek-chat",
        api_base=hostile_server(mode),
        timeout_s=5.0,
    )

    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(
            client.complete([{"role": "user", "content": "hi"}], []), timeout=10.0
        )

    assert str(excinfo.value).strip(), "异常文本是空的 —— 用户看不到任何线索"


async def test_unreachable_provider_raises_a_real_error() -> None:
    """连不上也要有明确异常（而不是挂死或返回空）。"""
    client = LiteLLMClient(model="deepseek/deepseek-chat", api_base=_closed_port_url())

    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(
            client.complete([{"role": "user", "content": "hi"}], []), timeout=20.0
        )

    assert str(excinfo.value).strip()


# --------------------------------------------------------------------------
# ⭐ PRE-1c：默认日志里不许出现堆栈
# --------------------------------------------------------------------------


class _ExplodingClient:
    """一个一定失败的 LLM 客户端，用来驱动主循环的错误路径。"""

    model = "stub"

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def complete(self, _messages: Any, _tools: Any) -> Any:
        raise self._exc


def _run_failing_diagnose(exc: BaseException) -> Any:
    """用真主循环跑一次注定失败的诊断。"""
    from fivewhys.agent import diagnose
    from fivewhys.mock.logstore import LogStore
    from fivewhys.tools import DataSource, build_registry

    registry = build_registry(DataSource.logs_only(LogStore()))
    settings = Settings(_env_file=None, max_steps=1, max_cost_usd=1.0)  # type: ignore[call-arg]

    return asyncio.run(
        diagnose(
            scenario_id="s",
            question="q",
            registry=registry,
            llm=_ExplodingClient(exc),
            settings=settings,
        )
    )


def test_default_logging_does_not_dump_tracebacks(caplog: pytest.LogCaptureFixture) -> None:
    """⭐ 默认级别下：主循环只打**一行** WARNING，且**不带堆栈**。

    上线前实测：坏 key 会在结果表格前甩出 60 行 litellm 内部 traceback。

    这里驱动的是**真主循环**，不是手搓一个 logger 调用 ——
    断言的是「错误路径实际写了什么日志」。
    """
    import logging as std_logging

    configure_logging(verbose=False)
    caplog.set_level(std_logging.WARNING, logger="fivewhys.agent.loop")

    run = _run_failing_diagnose(RuntimeError("provider said 401"))

    assert run.stop_reason == "error"
    assert "401" in (run.error or "")

    warnings = [record for record in caplog.records if record.levelno == std_logging.WARNING]
    assert len(warnings) == 1, f"默认级别下应该只有一行 WARNING，实际 {len(warnings)} 条"
    assert warnings[0].exc_info is None, "默认级别下不该带堆栈"
    assert "401" in warnings[0].getMessage()


def test_verbose_logging_keeps_the_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """``-v`` 时堆栈必须还在 —— 否则排查时没东西可看。"""
    import logging as std_logging

    configure_logging(verbose=True)
    caplog.set_level(std_logging.DEBUG, logger="fivewhys.agent.loop")

    _run_failing_diagnose(RuntimeError("provider said 401"))

    debug_records = [
        record
        for record in caplog.records
        if record.levelno == std_logging.DEBUG and record.exc_info is not None
    ]
    assert debug_records, "-v 时没有带堆栈的 DEBUG 记录 —— 排查时看不到原因"
    configure_logging(verbose=False)


def test_noisy_libraries_are_quiet_by_default() -> None:
    """litellm 的 INFO 横幅默认压掉 —— 它对调试有用，对用户是噪声。"""
    import logging as std_logging

    configure_logging(verbose=False)
    assert std_logging.getLogger("LiteLLM").level >= std_logging.CRITICAL

    configure_logging(verbose=True)
    assert std_logging.getLogger("LiteLLM").level <= std_logging.DEBUG
    configure_logging(verbose=False)


def test_litellm_print_banner_is_suppressed() -> None:
    """litellm 的 "Give Feedback / Get Help" 横幅走的是 ``print``，不是 logging。

    所以压 logger 级别对它没用 —— 必须设 ``suppress_debug_info``。
    实测：坏 key 时这段横幅会夹在我们的错误行和结果表格之间。
    """
    import litellm

    litellm.suppress_debug_info = False
    LiteLLMClient(model="m")
    assert litellm.suppress_debug_info is True


# --------------------------------------------------------------------------
# ⭐ PRE-1d：失败原因要说人话（demo 的 explain_failure）
# --------------------------------------------------------------------------


def _load_demo_module() -> Any:
    """把 scripts/demo_m1.py 当模块加载（它不是包的一部分）。"""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "demo_m1.py"
    spec = importlib.util.spec_from_file_location("demo_m1_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("error", "expected_hint"),
    [
        ("AuthenticationError: 401 Unauthorized", "API Key"),
        ("RateLimitError: 429 rate limit exceeded", "限流"),
        ("APITimeoutError: request timed out", "超时"),
        ("APIConnectionError: connection refused", "连不上"),
        ("InsufficientBalanceError: 402 balance", "余额"),
        ("SomethingWeirdError: ???", "-v"),
    ],
)
def test_failure_explains_what_to_do(error: str, expected_hint: str) -> None:
    """每种坏法都要给一句**可执行的**提示，而不是只说「error」。"""
    from fivewhys.models import AgentRun

    demo = _load_demo_module()
    run = AgentRun(scenario_id="s", model="m", stop_reason="error", error=error)

    text = demo.explain_failure(run)

    assert expected_hint in text, f"{error} 的提示里没有「{expected_hint}」：{text}"
    assert error.split(":")[0] in text, "提示里要带上原始错误类型，便于搜索"


def test_failure_without_detail_still_says_something() -> None:
    from fivewhys.models import AgentRun

    demo = _load_demo_module()
    run = AgentRun(scenario_id="s", model="m", stop_reason="max_steps")

    assert demo.explain_failure(run) == "max_steps"
