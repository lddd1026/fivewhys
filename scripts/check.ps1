<#
.SYNOPSIS
    本地验证 —— 提交代码之前必须跑这个。

.DESCRIPTION
    按顺序做三件事：
      1. 代码风格检查（ruff）
      2. 测试（pytest）
      3. 类型检查（mypy，失败不阻断）

    任何一步失败就退出，不会提交没验证过的代码。

.EXAMPLE
    pwsh -File scripts/check.ps1
#>

$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# UTF-8，避免中文测试输出乱码
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path (Split-Path -Parent $ProjectRoot) '.tmp'
$env:TMP = $env:TEMP

$Py = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Write-Host "找不到虚拟环境。先跑：pwsh -File scripts/setup.ps1" -ForegroundColor Red
    exit 1
}

$failed = $false

function Step($Name, $Block) {
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  -> 失败 (exit $LASTEXITCODE)" -ForegroundColor Red
        $script:failed = $true
    } else {
        Write-Host "  -> 通过" -ForegroundColor Green
    }
}

Step '代码风格 (ruff)' {
    & $Py -m ruff check .
}

Step '格式检查 (ruff format)' {
    & $Py -m ruff format --check .
}

Step '测试 (pytest)' {
    & $Py -m pytest
}

Step '类型检查 (mypy)' {
    & $Py -m mypy src
}

Write-Host ''
if ($failed) {
    Write-Host "验证未通过，先修好再提交。" -ForegroundColor Red
    exit 1
}
Write-Host "全部通过，可以提交了。" -ForegroundColor Green
