"""FIV-12 场景快照：把评测集固化成可校验的指纹。

需求 FR-4 要求「相同的场景标识必须产生逐字节一致的数据」。
这个脚本是那条要求的手工入口，另有等价命令 ``fivewhys snapshot``。

用法::

    python scripts/snapshot_scenarios.py            # 重造全部场景 + 重拍快照
    python scripts/snapshot_scenarios.py --check    # 只校验，不写任何文件
    python scripts/snapshot_scenarios.py --seed 3   # 换一套种子

**它不调 LLM，不联网，不花钱。**

拍完之后 ``data/scenarios/`` 里是场景包（生成物，在 .gitignore 里），
``eval/scenario_snapshot.json`` 是指纹清单（进版本库）——
后者才是「评测集没被改过」的证据。

脚本本身**不含任何逻辑**，只是把参数转给 ``fivewhys snapshot``。
逻辑写在 :mod:`fivewhys.snapshot` 里，这样命令行、脚本和测试跑的是同一份实现 ——
「落盘的」和「校验的」必须是同一套代码，否则校验就失去意义了。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能直接运行（不必先 pip install -e .）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fivewhys.cli import app  # noqa: E402
from fivewhys.scenario import DEFAULT_SCENARIO_ROOT  # noqa: E402
from fivewhys.snapshot import DEFAULT_SNAPSHOT_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="场景快照：固化并校验评测集")
    parser.add_argument("--seed", type=int, default=0, help="随机种子（默认 0）")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_SCENARIO_ROOT,
        help=f"场景包输出目录（默认 {DEFAULT_SCENARIO_ROOT}）",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help=f"快照文件路径（默认 {DEFAULT_SNAPSHOT_PATH}）",
    )
    parser.add_argument("--check", action="store_true", help="只校验已有快照，不写任何文件")
    args = parser.parse_args(argv)

    forwarded = [
        "snapshot",
        "--seed",
        str(args.seed),
        "--out",
        str(args.out),
        "--manifest",
        str(args.manifest),
    ]
    if args.check:
        forwarded.append("--check")

    # standalone_mode（默认）下，typer 自己负责打印和退出码。
    app(forwarded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
