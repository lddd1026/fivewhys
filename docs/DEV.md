# 开发环境说明

## 铁律：不在 C 盘安装任何文件

项目相关的**所有**内容都留在本项目所在的盘。安装脚本 `scripts/setup.ps1` 已经把这些都处理好了：

| 内容 | 位置 | 怎么保证的 |
| ---- | ---- | ---------- |
| 虚拟环境 | `<项目>\.venv` | 跟随项目盘 |
| pip 缓存 | `e:\pip_cache` | 写进 `.venv\pip.ini` 的 `cache-dir` |
| pip 构建临时目录 | `<项目父目录>\.tmp` | 安装时设置 `TEMP` / `TMP` 环境变量 |
| 包索引 | 清华镜像 | 写进 `.venv\pip.ini` 的 `index-url` |

## 中文乱码

Windows 上 Python 的输出一旦被**重定向**（管道、写文件、被别的程序捕获），
就会退回系统默认的 GBK 编码，中文变乱码。终端里直接跑没事，但管道里会。

解决：始终带上 `PYTHONUTF8=1`。

```powershell
$env:PYTHONUTF8 = '1'
```

`scripts/setup.ps1` 已经设了。如果想一劳永逸，加进 PowerShell 配置文件：

```powershell
# 查看配置文件路径
$PROFILE

# 追加一行
Add-Content -Path $PROFILE -Value '$env:PYTHONUTF8 = "1"'
```

> **注意**：`TEMP` 是环境变量，没法写进 `pip.ini`。所以每次手动装包时，
> 要么先跑 `scripts/setup.ps1`，要么自己设一下：
>
> ```powershell
> $env:TEMP = 'D:\deepseekharness\.tmp'
> $env:TMP  = 'D:\deepseekharness\.tmp'
> .\.venv\Scripts\python.exe -m pip install <包名>
> ```

## 常用命令

```powershell
# 一键装环境（幂等，可重复跑）
pwsh -File scripts/setup.ps1

# 激活
.\.venv\Scripts\Activate.ps1

# 体检
fivewhys doctor

# 跑测试
pytest

# 代码检查 / 格式化
ruff check .
ruff format .

# 类型检查
mypy src
```

## 为什么走镜像

`files.pythonhosted.org` 在本地网络下连接极不稳定（SSL EOF / 连接重置），
官方源装 litellm 这种依赖大户基本必失败。清华镜像实测稳定。

要用回官方源：

```powershell
pwsh -File scripts/setup.ps1 -IndexUrl https://pypi.org/simple
```

## 换 Python 解释器

脚本默认用 PATH 里的 `python`（当前是 `E:\ProgramData\anaconda3\python.exe`，3.13.9）。
想固定解释器，先建好 venv 再跑脚本即可 —— 脚本检测到 `.venv` 存在就直接复用。

## 端到端测试：本地假 LLM 服务

`tests/mock_llm_server.py` 起一个本地的 OpenAI 兼容端点（用标准库，零依赖）。
把 `FIVEWHYS_API_BASE` 指向它，就能在**不联网、不花钱**的前提下跑通整条链路：

```
demo_m1.py 子进程 -> get_settings -> LiteLLMClient -> litellm 发真实 HTTP
  -> 假服务返回响应 -> 解析 -> 工具分发 -> query_logs 真的被调用
  -> 提交结论 -> 判分 -> 输出表格与判定
```

**除「模型智力」以外全部是真的。** 比假 Python 对象（`ScriptedLLM`）更进一步：
它能抓到 HTTP 层、JSON 序列化、litellm 适配层、进程边界、退出码的问题。

`pytest tests/test_e2e_demo.py` 就是在跑这个。

手动体验：

```powershell
$env:DEEPSEEK_API_KEY = 'sk-fake'
$env:FIVEWHYS_API_BASE = 'http://127.0.0.1:PORT'   # 由 fake_llm_server 提供
python scripts/demo_m1.py --runs 3 --trace
```

### 但它不能替代真实运行

响应是脚本化的，不会推理。它证明的是「链路通」，不是「模型够聪明」。
**M1 的验收标准（跑 5 次至少 3 次正确）仍然需要真实的 API Key。**

### 踩过的坑

- 环境变量前缀是 **`FIVEWHYS_`**（five + whys）。写成 `FIVWHYS_` 不会报错，
  只会静默失效 —— 请求会打到官方接口上，然后收到莫名其妙的鉴权失败。
  `tests/test_e2e_demo.py::test_env_var_prefix_is_fivewhys` 守着这一点。
- 假服务用 HTTP/1.1 + `Content-Length`，注意别漏 `Content-Length` 否则客户端会一直等。

## 看看工具给 agent 看了什么

工具返回值会直接进模型的上下文，所以「agent 到底看到了什么」是最该亲眼确认的事。
这个脚本把 5 个工具的**原始返回**原样打印出来，不调用任何 LLM：

```powershell
python scripts/inspect_scenario.py                    # 用 data/scenarios 里的场景包
python scripts/inspect_scenario.py -c dependency_5xx  # 换一种故障
python scripts/inspect_scenario.py -c no_fault        # 健康场景
python scripts/inspect_scenario.py --full             # 不截断输出
```

另一个脚本专门用来看**说明书**（也就是提示词里那部分）：

```powershell
python scripts/review_tool_descriptions.py            # 通读 5 条描述 + 全部参数描述
python scripts/review_tool_descriptions.py --budget   # 只看总量：描述 2167 字 ≈ 722 token/次请求
```

超预算时它会以退出码 1 结束。**该做的是删废话，不是调高预算数字** ——
说明书写在每一次请求的提示词里，30 步的诊断就发 30 遍，这笔钱直接落在成本红线（NFR-2）上。

`inspect_scenario.py` 按一次真实排障的顺序调用工具（指标 → 日志 → 配置 → 发布 → 拓扑），
最后自检两件事：日志里没有答案词、答案能在配置里查到。

**读一遍输出**，你就能判断：线索够不够、哪一步查了等于没查、
答案会不会不小心从日志里泄漏出去。

## 场景快照：证明评测集没被改过

```powershell
# 重造全部场景 + 重拍快照（不调 LLM，不联网，不花钱）
python scripts/snapshot_scenarios.py           # 等价于 fivewhys snapshot

# 只校验，不写任何文件 —— 刚 clone 下来、data/ 还是空的也能跑
python scripts/snapshot_scenarios.py --check   # 等价于 fivewhys snapshot --check

fivewhys faults                                # 看有哪些故障可注入
fivewhys build-scenario -c memory_leak         # 只造一个，调试用
```

两份东西，用途完全不同：

| 位置 | 内容 | 进版本库吗 |
| ---- | ---- | ---------- |
| `data/scenarios/` | 场景包（日志 / 指标 / 配置 / 发布） | ❌ 生成物，`.gitignore` 了 |
| `eval/scenario_snapshot.json` | 每个场景的 SHA-256 指纹 | ✅ **这才是证据** |

校验比对的是「代码 + 种子 → 字节」，**不读磁盘上的场景包**。
所以删掉 `data/` 目录也能校验通过，校验也不会往临时目录（尤其 C 盘）写东西。

### 快照测试红了怎么办

`tests/test_snapshot.py::test_committed_snapshot_matches_current_code` 会失败。
**它红不代表代码写错了 —— 代表评测集变了。**

1. 先确认改动是不是有意的（确实加了新故障？确实改了注入器描述的现象？）
2. 是有意的 → `python scripts/snapshot_scenarios.py` 重拍，和代码一起提交
3. 不是有意的 → `git diff src/fivewhys/mock/` 看谁动过场景数据

**不要直接删掉快照文件了事。** M7 的改进曲线只有在「两次跑的是同一套场景」
时才可比 —— 这道闸门就是为那件事存在的。
