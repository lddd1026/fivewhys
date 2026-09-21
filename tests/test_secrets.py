"""上线前的「别把密钥推上去」机器化。

用户的原话是「提交前记得把 apikey 删掉，别被别人拿去用了」。
靠人记得的事迟早会忘一次 —— 而**密钥泄漏只需要成功一次**。
所以这里把那条纪律变成每次 `pytest` 都会跑的检查。

## 两条不变量

1. **`.env` 不被跟踪**：它是本机放凭据的地方，也是唯一该出现真实密钥的文件。
2. **被跟踪的文件里没有密钥样式的串**：这才是「会被推上去的东西」的集合。

## 为什么模式里**不带连字符**（`sk-[A-Za-z0-9]{20,}`）

真实的 OpenAI / DeepSeek key 是 `sk-` + 一长串**字母数字**。
而测试里用的假值都带连字符（`sk-fake-for-local-test`、`sk-from-dotenv`）——
所以这个模式能自动放过假值、抓住真值，**不需要维护白名单**。

反过来，如果写成 `sk-[A-Za-z0-9-]{20,}`，上面那个假值（19 个字符 + 连字符）
迟早会因为某次改动多一个字符就误报 —— **会叫狼来了的检查等于没有检查**。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 各类密钥的样式。宁可窄一点：宽模式误报几次，大家就会开始无视它。
SECRET_PATTERNS = {
    "OpenAI/DeepSeek 风格": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "GitHub PAT": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "AWS Access Key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "私钥文件头": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# 这些文件是**给人看的例子**，允许出现「像密钥的字符串」。
# 加进来的每个都要能说清为什么安全。
ALLOWED = {
    # 审查报告里把搜索模式当文本引用（`sk-[0-9a-f]{16,}`），不是密钥
    "docs/RELEASE_REVIEW.md": {"OpenAI/DeepSeek 风格"},
    # 本文件自己就写着这些模式
    "tests/test_secrets.py": set(SECRET_PATTERNS),
}


def _tracked_files() -> list[str]:
    """``git ls-files`` —— 「会被推上去的东西」的准确集合。"""
    result = subprocess.run(  # noqa: S603 —— 参数是我们自己拼的
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("不是 git 仓库（例如从 wheel 安装的副本），跳过密钥扫描")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_env_is_never_tracked() -> None:
    """`.env` 是本机放凭据的地方，**绝不能**进入版本库。"""
    result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert result.returncode != 0, ".env 被 git 跟踪了 —— 凭据要跟着提交一起上去了"


def test_no_secret_looking_strings_in_tracked_files() -> None:
    """被跟踪的文件里不许出现密钥样式的串。

    红了先别改这个测试：先确认那串东西是不是真的凭据，
    是的话要**从那行代码里删掉并轮换密钥**（历史里还有一份，删文件不够）。
    """
    findings: list[str] = []

    for name in _tracked_files():
        path = PROJECT_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制文件不扫

        for label, pattern in SECRET_PATTERNS.items():
            if label in ALLOWED.get(name.replace("\\", "/"), set()):
                continue
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{name}:{line}  {label}  {match.group()[:12]}…")

    assert findings == [], "被跟踪的文件里出现疑似密钥：\n  " + "\n  ".join(findings)


def test_the_scanner_actually_catches_a_real_looking_key() -> None:
    """守住这个测试本身 —— 一个永远通过的安全检查等于不存在。

    用一个**真密钥的形状**（字母数字、够长）验证模式确实会命中。
    """
    sample = "DEEPSEEK_API_KEY=" + "sk-" + "0123456789abcdef0123456789abcdef"

    assert SECRET_PATTERNS["OpenAI/DeepSeek 风格"].search(sample), "模式抓不住真密钥的形状"


def test_the_scanner_ignores_the_fake_values_used_in_tests() -> None:
    """同时守住另一头：测试里的假值不能被误报。

    会叫狼来了的检查，最后一定会被无视 —— 那比没有检查更糟。
    """
    for fake in ("sk-fake-for-local-test", "sk-from-dotenv", "sk-CANARY-bad-key-for-probe"):
        assert not SECRET_PATTERNS["OpenAI/DeepSeek 风格"].search(fake), f"误报了假值：{fake}"
