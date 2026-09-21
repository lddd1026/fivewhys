"""场景快照 —— 给评测集拍一张**字节级指纹**，证明它可复现、也没被悄悄改过。

## 要解决的问题（需求 FR-4 / NFR-1）

需求里写了「相同的场景必须产生逐字节一致的数据」，但**「写在文档里的承诺」
和「机器每次都能验证的事实」是两回事**。这个模块把承诺变成事实：

::

    代码 + 种子  --构造-->  场景包  --SHA-256-->  指纹
                             ↑                      │
                             └──── 必须相等 ────────┘

## 为什么指纹要进版本库

场景包本身（``data/scenarios/``）是**生成物**，进了 ``.gitignore`` ——
它随时能用一条命令重造，没必要占版本库。

但**指纹必须提交**，因为它是「评测集没变」的唯一证据：

1. **防悄悄漂移**。M7 要比较「改进前 vs 改进后」的准确率。
   如果有人顺手调了一下注入器里的日志条数，评测集就变了，
   曲线也就不可信了 —— 而这种改动在 diff 里只显示成几行无关紧要的代码。
   指纹一红，你就被迫先承认「我改了评测集」。
2. **防环境差异**。指纹跨平台一致（强制 LF 换行），
   所以「在我机器上是好的」这句话可以当场验证。

## 三个动作

============  ==========================================================
:func:`take_snapshot`   构造全部场景 + 落盘 + 算指纹
:func:`save_snapshot`   把指纹写成 JSON（提交进版本库的就是它）
:func:`verify_snapshot` 用**当前代码**重造一遍，和指纹比对
============  ==========================================================

## 设计细节：为什么快照文件里没有「生成时间」

因为快照文件本身也必须**逐字节可复现**。加一个 ``generated_at``
等于自己破坏自己保证的东西 —— 每次重跑指纹文件都变，就没人能判断
「变的是数据还是时间戳」。需要时间的话，``git log`` 里有。

## 设计细节：为什么记每个文件的指纹，而不只记一个总指纹

因为报错要**指得出地方**。总指纹不一致只能告诉你「变了」，
逐文件指纹能告诉你「变的是 ``configs.jsonl``」—— 直接指向根因证据。
和这个项目其它地方一样：宁可多存一点，也要能讲清楚。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from fivewhys.mock.injectors import available
from fivewhys.models import FaultCategory
from fivewhys.scenario import (
    DEFAULT_BASE_TIME,
    DEFAULT_SCENARIO_ROOT,
    MANIFEST_NAME,
    PACKAGE_FILES,
    Scenario,
    ScenarioManifest,
    build_all_scenarios,
)

# 快照文件的格式版本。
SNAPSHOT_VERSION = 1

# 快照文件默认落在这里 —— **进版本库**，见模块开头的说明。
DEFAULT_SNAPSHOT_PATH = Path("eval/scenario_snapshot.json")


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


def hash_text(text: str) -> str:
    """文本的 SHA-256。统一走这里，避免各处写法不一致。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def combine_digests(items: Iterable[tuple[str, str]]) -> str:
    """把若干「名字 -> 指纹」合成一个总指纹。

    先按名字排序再拼 —— 文件系统的遍历顺序不稳定，
    不排序的话同样的数据会算出不同的总指纹。
    """
    payload = "\n".join(f"{name}:{digest}" for name, digest in sorted(items))
    return hash_text(payload)


class ScenarioDigest(BaseModel):
    """一个场景包的指纹。"""

    scenario_id: str
    fault_category: FaultCategory
    digest: str = Field(description="场景包的总体指纹")
    files: dict[str, str] = Field(
        default_factory=dict,
        description="逐文件指纹（文件名 -> SHA-256），用来指出「变的是哪份数据」",
    )
    total_bytes: int = 0

    @property
    def file_names(self) -> list[str]:
        return sorted(self.files)


class Snapshot(BaseModel):
    """全部评测场景的指纹清单。"""

    version: int = SNAPSHOT_VERSION
    seed: int
    base_time: datetime
    scenarios: list[ScenarioDigest] = Field(default_factory=list)

    @property
    def digest(self) -> str:
        """覆盖全部场景的总指纹。"""
        return combine_digests((entry.scenario_id, entry.digest) for entry in self.scenarios)

    @property
    def short_digest(self) -> str:
        """总指纹的前 12 位，给人看的。"""
        return self.digest[:12]

    def entry(self, scenario_id: str) -> ScenarioDigest | None:
        for candidate in self.scenarios:
            if candidate.scenario_id == scenario_id:
                return candidate
        return None

    def summary(self) -> str:
        return (
            f"快照 v{self.version} · 种子 {self.seed} · {len(self.scenarios)} 个场景 · "
            f"指纹 {self.short_digest}"
        )


# --------------------------------------------------------------------------
# 算指纹
# --------------------------------------------------------------------------


def _digest_from(
    scenario_id: str, category: FaultCategory, files: Mapping[str, str]
) -> ScenarioDigest:
    """从一个场景包的全部文件内容算出指纹。"""
    per_file = {name: hash_text(text) for name, text in files.items()}
    return ScenarioDigest(
        scenario_id=scenario_id,
        fault_category=category,
        digest=combine_digests(per_file.items()),
        files=per_file,
        total_bytes=sum(len(text.encode("utf-8")) for text in files.values()),
    )


def digest_scenario(scenario: Scenario) -> ScenarioDigest:
    """算**内存里**这个场景的指纹。

    :func:`verify_snapshot` 走这条路 —— 不需要真的落盘，
    所以校验可以跑得很快，也不会往磁盘（尤其是 C 盘临时目录）写东西。
    """
    return _digest_from(
        scenario.scenario_id, scenario.ground_truth.fault_category, scenario.to_files()
    )


def digest_scenario_dir(path: Path) -> ScenarioDigest:
    """算**磁盘上**这个场景目录的指纹。

    和 :func:`digest_scenario` 的唯一区别是数据来源。
    用途不同：这个用来抓「文件被人动过」，那个用来抓「代码变了」。
    """
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"场景包里没有 {MANIFEST_NAME}：{path}")

    manifest = ScenarioManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    files = {
        name: (path / name).read_text(encoding="utf-8")
        for name in PACKAGE_FILES
        if (path / name).exists()
    }
    return _digest_from(manifest.scenario_id, manifest.ground_truth.fault_category, files)


# --------------------------------------------------------------------------
# 拍快照
# --------------------------------------------------------------------------


def build_snapshot(*, seed: int = 0, base_time: datetime | None = None) -> Snapshot:
    """构造全部场景并算指纹，**不落盘**。

    给测试和校验用 —— 校验只需要「代码 + 种子 → 字节」，
    完全不必往磁盘（尤其是 C 盘临时目录）写东西。
    """
    base = base_time or DEFAULT_BASE_TIME
    scenarios = build_all_scenarios(seed=seed, base_time=base)
    return Snapshot(
        seed=seed,
        base_time=base,
        scenarios=[
            digest_scenario(scenario)
            for scenario in sorted(scenarios, key=lambda item: item.scenario_id)
        ],
    )


def take_snapshot(
    root: Path | None = None,
    *,
    seed: int = 0,
    base_time: datetime | None = None,
    clean_stale: bool = True,
) -> tuple[Snapshot, list[Path]]:
    """构造全部已注册的场景，落盘，并算出指纹。

    Args:
        root: 场景包输出目录。
        seed: 随机种子。快照记录它 —— 换种子就是换了一套评测集。
        base_time: 时间线起点。
        clean_stale: 是否删掉目录里「不属于本次快照」的场景包。

    Returns:
        ``(快照, 被清掉的陈旧目录)``。

    ``clean_stale`` 默认打开是有原因的：M6 的评测台会遍历 ``data/scenarios/``
    下的所有目录。上一轮留下的场景包不会自己消失，它会**悄悄混进评测集**，
    让「20 个场景」变成 21 个而没人发现。生成目录就该是生成目录的样子。
    """
    target_root = root or DEFAULT_SCENARIO_ROOT
    base = base_time or DEFAULT_BASE_TIME

    scenarios = build_all_scenarios(seed=seed, base_time=base)
    directories = [scenario.save(target_root) for scenario in scenarios]

    stale = _clean_stale(target_root, {path.name for path in directories}) if clean_stale else []

    digests = [
        digest_scenario_dir(path) for path in sorted(directories, key=lambda item: item.name)
    ]
    return Snapshot(seed=seed, base_time=base, scenarios=digests), stale


def _clean_stale(root: Path, keep: set[str]) -> list[Path]:
    """删掉 root 下不属于本次快照的场景包目录。

    只删「看起来就是场景包」的目录（含 ``scenario.json``）。
    目录里如果放着别的东西，宁可留着也不动 —— 清理脚本误删东西是最糟的失败方式。
    """
    if not root.exists():
        return []

    removed: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in keep:
            continue
        if not (child / MANIFEST_NAME).exists():
            continue
        for leftover in child.iterdir():
            leftover.unlink()
        child.rmdir()
        removed.append(child)
    return removed


def validate_all_scenarios(*, seed: int = 0, base_time: datetime | None = None) -> list[str]:
    """构造全部已注册场景并逐个 :meth:`Scenario.validate`，返回问题列表。

    「场景能生成」和「场景可用」是两件事：日志泄漏答案、配置历史里没有变化，
    这些数据照样能生成，但拿去评测只会得到一堆看不懂的失败。
    """
    problems: list[str] = []
    for scenario in build_all_scenarios(seed=seed, base_time=base_time):
        problems.extend(f"{scenario.scenario_id}：{problem}" for problem in scenario.validate())
    return problems


# --------------------------------------------------------------------------
# 存 / 读
# --------------------------------------------------------------------------


def save_snapshot(snapshot: Snapshot, path: Path | None = None) -> Path:
    """写入快照文件。"""
    target = path or DEFAULT_SNAPSHOT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        snapshot.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def load_snapshot(path: Path | None = None) -> Snapshot:
    """读回快照文件。"""
    target = path or DEFAULT_SNAPSHOT_PATH
    if not target.exists():
        raise FileNotFoundError(f"没有快照文件：{target}。先跑一次 `fivewhys snapshot` 生成它。")
    return Snapshot.model_validate_json(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def verify_snapshot(snapshot: Snapshot) -> list[str]:
    """用**当前代码**重造全部场景，和快照比对。返回问题列表，空列表表示通过。

    这是 FR-4 的可执行判据。它会抓四类问题：

    1. 注册表里新增/删除了故障，快照没跟上 —— 评测集的范围变了
    2. 某个场景的数据变了 —— 注入器或数据层被改动，指得出是哪份文件
    3. 场景标识变了 —— 同一个故障换了 id，历史结果没法对齐
    4. 场景数据本身不可用（日志泄漏答案、配置历史没变化）—— 见
       :func:`validate_all_scenarios`

    ⚠️ 这里**不读磁盘**：比对的是「代码 + 种子 → 字节」，
    磁盘上的 ``data/`` 只是它的产物，删了也能重造。
    """
    problems: list[str] = list(
        validate_all_scenarios(seed=snapshot.seed, base_time=snapshot.base_time)
    )

    # 先查快照文件**自身**是否自洽。
    #
    # 为什么必须有这一步：逐文件指纹是拿来「指认变了哪份文件」的，
    # 如果它被人改过而总指纹没改，那么比对时总指纹仍然相等 —— 校验会**通过**，
    # 但报错信息从此会指错地方。这种「校验器自己不可信」的失效最隐蔽。
    # 这个洞是 FIV-12 手工篡改一份快照副本时发现的（见 TASKS.md）。
    for entry in snapshot.scenarios:
        if entry.files and combine_digests(entry.files.items()) != entry.digest:
            problems.append(
                f"快照文件自身不一致：「{entry.scenario_id}」的逐文件指纹加不出它的总指纹"
                f" —— 快照文件被手工改过或已损坏，请重拍：fivewhys snapshot"
            )

    covered = {entry.fault_category for entry in snapshot.scenarios}
    registered = set(available())

    for category in sorted(registered - covered, key=lambda item: item.value):
        problems.append(
            f"快照里没有故障「{category.value}」—— 新增故障后要重拍快照：fivewhys snapshot"
        )
    for category in sorted(covered - registered, key=lambda item: item.value):
        problems.append(
            f"快照里有故障「{category.value}」，但代码里已经没有了 —— 评测集缩小了，"
            f"确认无误后重拍快照：fivewhys snapshot"
        )

    for scenario in build_all_scenarios(seed=snapshot.seed, base_time=snapshot.base_time):
        expected = snapshot.entry(scenario.scenario_id)
        if expected is None:
            problems.append(
                f"新造出来的场景「{scenario.scenario_id}」不在快照里 —— 场景标识变了，"
                f"同一故障的历史评测结果将无法对齐"
            )
            continue

        actual = digest_scenario(scenario)
        if actual.digest == expected.digest:
            continue

        changed = sorted(
            name
            for name in actual.file_names
            if name in expected.files and actual.files[name] != expected.files[name]
        )
        added = [name for name in actual.file_names if name not in expected.files]
        missing = [name for name in expected.file_names if name not in actual.files]
        details = (
            [f"{name} 内容变了" for name in changed]
            + [f"{name} 新增" for name in added]
            + [f"{name} 消失" for name in missing]
        )
        problems.append(
            f"「{scenario.scenario_id}」的数据变了：{'、'.join(details) or '未定位到具体文件'}"
            f" —— 评测集被改动了，确认是有意为之再重拍快照"
        )

    return problems


__all__ = [
    "DEFAULT_SNAPSHOT_PATH",
    "SNAPSHOT_VERSION",
    "ScenarioDigest",
    "Snapshot",
    "build_snapshot",
    "combine_digests",
    "digest_scenario",
    "digest_scenario_dir",
    "hash_text",
    "load_snapshot",
    "save_snapshot",
    "take_snapshot",
    "validate_all_scenarios",
    "verify_snapshot",
]
