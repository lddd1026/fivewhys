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
