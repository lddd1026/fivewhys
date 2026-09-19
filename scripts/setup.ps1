<#
.SYNOPSIS
    fivewhys 开发环境一键安装。

.DESCRIPTION
    约定：**本项目不在 C 盘安装任何文件。**

    具体做法：
      - 虚拟环境建在项目目录下（.venv），跟随项目所在盘
      - pip 缓存固定指向 e:\pip_cache（已存在的本地缓存，复用省钱）
      - pip 的临时构建目录指向 <项目父目录>\.tmp，避开 C:\...\Temp
      - 包索引走清华镜像，避免 files.pythonhosted.org 连接不稳

    幂等：可以重复运行。

.EXAMPLE
    pwsh -File scripts/setup.ps1
#>

[CmdletBinding()]
param(
    # pip 缓存目录。默认复用已有的 E 盘缓存
    [string]$CacheDir = 'e:\pip_cache',

    # 包索引。想用官方源就传 https://pypi.org/simple
    [string]$IndexUrl = 'https://pypi.tuna.tsinghua.edu.cn/simple',

    # 是否安装 dev 依赖（pytest / ruff / mypy）
    [switch]$NoDev
)

$ErrorActionPreference = 'Stop'

# 项目根目录 = 本脚本的上一级
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
Write-Host "项目根目录: $ProjectRoot" -ForegroundColor Cyan

# ---- 0. 强制 UTF-8 ----
# Windows 上 Python 输出被重定向（管道 / 写文件）时会退回 GBK，中文变乱码。
# PYTHONUTF8=1 让解释器始终用 UTF-8，终端和管道都正常。
$env:PYTHONUTF8 = '1'

# ---- 1. 临时目录挪出 C 盘 ----
$TempRoot = Join-Path (Split-Path -Parent $ProjectRoot) '.tmp'
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
$env:TEMP = $TempRoot
$env:TMP = $TempRoot
Write-Host "TEMP -> $TempRoot" -ForegroundColor Cyan

# ---- 2. pip 缓存 ----
if (-not (Test-Path $CacheDir)) {
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
}
$env:PIP_CACHE_DIR = $CacheDir
Write-Host "PIP_CACHE_DIR -> $CacheDir" -ForegroundColor Cyan

# ---- 3. 虚拟环境 ----
$VenvPy = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Host "创建虚拟环境 .venv ..." -ForegroundColor Yellow
    python -m venv (Join-Path $ProjectRoot '.venv')
} else {
    Write-Host "复用已存在的 .venv" -ForegroundColor Yellow
}

# ---- 4. 把镜像配置写进 venv（site-level 配置，不写 C 盘）----
$TrustedHost = ([Uri]$IndexUrl).Host
$PipIni = @"
[global]
index-url = $IndexUrl
trusted-host = $TrustedHost
cache-dir = $CacheDir
timeout = 60
retries = 10

[install]
upgrade-strategy = only-if-needed
"@
Set-Content -Path (Join-Path $ProjectRoot '.venv\pip.ini') -Value $PipIni -Encoding utf8
Write-Host "已写入 .venv\pip.ini（镜像: $IndexUrl）" -ForegroundColor Cyan

# ---- 5. 安装 ----
Write-Host "`n升级 pip ..." -ForegroundColor Yellow
& $VenvPy -m pip install --upgrade pip

$Spec = if ($NoDev) { '.' } else { '.[dev]' }
Write-Host "`n安装 $Spec ..." -ForegroundColor Yellow
& $VenvPy -m pip install -e $Spec

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n安装失败（exit $LASTEXITCODE）" -ForegroundColor Red
    exit $LASTEXITCODE
}

# ---- 6. 验证 ----
Write-Host "`n=== 验证 ===" -ForegroundColor Green
& $VenvPy -c @"
import fivewhys, pydantic
print('fivewhys ', fivewhys.__version__)
print('pydantic ', pydantic.VERSION)
try:
    import litellm
    print('litellm  ', litellm.__version__)
except ImportError:
    print('litellm   (未安装)')
"@

Write-Host "`n安装完成。下一步：" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1   # 激活环境"
Write-Host "  fivewhys doctor                # 体检"
Write-Host "  pytest                         # 跑测试"
