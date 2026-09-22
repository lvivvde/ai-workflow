<#
.SYNOPSIS
  Windows 原生验收脚本：检查依赖、建索引、跑 MCP smoke test，并按 Hardware Profile 写运行记录。

.DESCRIPTION
  在 Windows 10/11 上一次性完成 V2-11 需要的验收动作，全程不联网、不下载任何模型：

  1. 检查 Python / uv / Git 版本与前缀；
  2. 打印 capability doctor（能力包、磁盘、Tesseract 兼容回退与语言包、模型仓库路径）；
  3. 用本仓库或指定资料目录重建共享索引（可跳过）；
  4. 通过 stdio 启动 MCP server 并调用 index_status（tools/smoke_stdio.py）；
  5. 对 baseline / recommended / visual 三个 Profile 各跑一次 baseline，把运行记录追加到
     <Output>\<profile>.jsonl（含延迟、峰值内存、磁盘与降级事件）。

  缺少能力包只会让对应 Profile 的记录多出降级事件，脚本本身不会因此失败。

.PARAMETER Source
  索引的源资料目录，默认仓库根目录。

.PARAMETER IndexDirectory
  共享索引目录，默认 .index\knowledge。

.PARAMETER Output
  运行记录目录，默认 .baseline\windows。

.PARAMETER SkipIndex
  跳过第 3 步的索引重建，只做检查与测量。

.EXAMPLE
  .\scripts\windows-smoke.ps1
  .\scripts\windows-smoke.ps1 -Source "D:\GameProject\DesignDocuments" -SkipIndex
#>

[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$IndexDirectory = ".index\knowledge",
    [string]$Output = ".baseline\windows",
    [switch]$SkipIndex
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

function Get-CommandPath {
    param([string]$Name, [switch]$Required)
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $found) {
        if ($Required) {
            throw "缺少必需命令：$Name"
        }
        Write-Host "可选依赖缺失：$Name"
        return ""
    }
    return $found.Source
}

function Invoke-Python {
    param([string[]]$Arguments, [switch]$AllowFailure)
    & $python @Arguments
    if (-not $AllowFailure -and $LASTEXITCODE -ne 0) {
        throw "python $($Arguments -join ' ') 退出码 $LASTEXITCODE"
    }
}

try {
    Write-Host "== 1. 前置依赖 =="
    Get-CommandPath -Name "git" -Required | Out-Null
    $uv = Get-CommandPath -Name "uv"
    $python = Get-CommandPath -Name "python" -Required
    Invoke-Python @("--version")
    if ($uv) {
        & $uv --version
    }
    $machine = [System.Environment]::Is64BitOperatingSystem
    Write-Host "OS: $([System.Environment]::OSVersion.VersionString) / 64 位：$machine"

    Write-Host "== 2. capability doctor =="
    $doctor = Join-Path $Output "doctor.json"
    New-Item -ItemType Directory -Force -Path $Output | Out-Null
    Invoke-Python @("-m", "game_design_knowledge.cli", "capability", "doctor") -AllowFailure |
        Out-File -Encoding utf8 $doctor
    Write-Host "已写入 $doctor"

    if (-not $SkipIndex) {
        Write-Host "== 3. 重建共享索引 =="
        $sourceArgument = if ($Source) { $Source } else { "." }
        Invoke-Python @(
            "-m", "game_design_knowledge.cli", "index", $sourceArgument,
            "--output", $IndexDirectory
        )
    }

    Write-Host "== 4. MCP stdio smoke test =="
    Invoke-Python @("tools\smoke_stdio.py", $IndexDirectory)

    Write-Host "== 5. 三种 Hardware Profile 的运行记录 =="
    foreach ($profile in @("baseline", "recommended", "visual")) {
        $records = Join-Path $Output "$profile.jsonl"
        Invoke-Python @(
            "-m", "game_design_knowledge.cli", "capability", "baseline",
            "--profile", $profile,
            "--index-dir", $IndexDirectory,
            "--output", $records
        ) | Out-Null
        Write-Host "已追加运行记录：$records"
    }

    Write-Host "验收完成。运行记录在 $Output，doctor 报告在 $doctor。"
}
finally {
    Pop-Location
}
