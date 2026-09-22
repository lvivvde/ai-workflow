<#
.SYNOPSIS
  Windows 分层基线：对 baseline / recommended / visual 三档各跑一次评测，产出 Evaluation Run Manifest 与门禁判定。

.DESCRIPTION
  在 Windows 10/11 上把 V2-12 的「首轮分层基线」变成一条可重复的命令，全程不联网：

  1. 检查 Python 与语料目录；
  2. 对 baseline / recommended / visual 三档各跑一次 tools/evaluate.py（--no-gates），
     写出 run.json / item_results.json / report.md，并把这一轮的 Run Manifest 抄一份到
     <Output>\<profile>\run.json；
  3. 用 tools/judge_gates.py 对每档的 Run Manifest 单独判门禁：同一档的上一轮
     <Output>\<profile>\previous-run.json 存在时就作为相对回退限制的基线；
  4. 把本轮 Run Manifest 归档为 <Output>\<profile>\previous-run.json，供下一轮比较。

  门禁失败不会让脚本失败，除非显式加 -RequireGates：没有装 OCR 引擎的机器上，
  OCR 层只能报 unavailable，那两条门槛按设计归入 environment 类（见
  evaluation/quality-gates.md）。invariant 违规永远让脚本失败。

.PARAMETER Corpus
  语料目录，可重复；默认三份随仓语料。

.PARAMETER RunsDir
  Evaluation Run Manifest 的落盘目录，默认 evaluation\runs（已被 .gitignore 忽略）。

.PARAMETER Output
  每档的 Run Manifest 副本、门禁判定与日志目录，默认 .baseline\evaluation。

.PARAMETER Gates
  门禁定义，默认 evaluation\quality-gates.json。

.PARAMETER Python
  运行评测的解释器。默认用仓库 .venv 里的 python，其次 uv run python，最后 PATH 上的 python。

.PARAMETER RequireGates
  门禁失败也让脚本以非零退出码结束。

.PARAMETER SkipEvaluation
  跳过第 2 步，只对已归档的 Run Manifest 重新判门禁（改过阈值后重判历史结果时用）。

.EXAMPLE
  .\scripts\windows-baseline.ps1
  .\scripts\windows-baseline.ps1 -RequireGates
  .\scripts\windows-baseline.ps1 -SkipEvaluation -Gates D:\controlled-eval\gates.json
#>

[CmdletBinding()]
param(
    [string[]]$Corpus = @(
        "evaluation\corpora\v1_compatibility",
        "evaluation\corpora\development_set",
        "evaluation\corpora\golden_set"
    ),
    [string]$RunsDir = "evaluation\runs",
    [string]$Output = ".baseline\evaluation",
    [string]$Gates = "evaluation\quality-gates.json",
    [string]$Python = "",
    [switch]$RequireGates,
    [switch]$SkipEvaluation
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$profiles = @("baseline", "recommended", "visual")
Push-Location $root

function Invoke-Python {
    param([string[]]$Arguments, [switch]$AllowFailure, [switch]$Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $invocation = @($pythonCommand) + @($Arguments)
        $executable = $invocation[0]
        $rest = @()
        if ($invocation.Count -gt 1) { $rest = $invocation[1..($invocation.Count - 1)] }
        $lines = & $executable @rest 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if (-not $Quiet) { $lines | Out-Host }
    if (-not $AllowFailure -and $code -ne 0) {
        throw "$($invocation -join ' ') 退出码 $code"
    }
    return [pscustomobject]@{
        code = $code
        text = ($lines -join [Environment]::NewLine)
    }
}

function Get-RunManifest {
    param([string]$Directory, [string]$Output)
    $match = [regex]::Match($Output, '"run_id":\s*"([^"]+)"')
    if (-not $match.Success) { return "" }
    $manifest = Join-Path (Join-Path $Directory $match.Groups[1].Value) "run.json"
    if (-not (Test-Path $manifest)) { return "" }
    return $manifest
}

try {
    Write-Host "== 1. 前置依赖与语料 =="
    if ($Python) {
        $pythonCommand = @($Python)
    }
    elseif (Test-Path (Join-Path $root ".venv\Scripts\python.exe")) {
        $pythonCommand = @(Join-Path $root ".venv\Scripts\python.exe")
    }
    elseif (Get-Command "uv" -ErrorAction SilentlyContinue) {
        $pythonCommand = @("uv", "run", "python")
    }
    elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
        $pythonCommand = @("python")
    }
    else {
        throw "找不到可用的解释器：请先 uv sync，或用 -Python 指定"
    }
    Invoke-Python -Arguments @("--version") -Quiet | Out-Null
    Write-Host "解释器：$($pythonCommand -join ' ')"
    if (-not (Test-Path $Gates)) { throw "门禁定义不存在：$Gates" }
    foreach ($path in $Corpus) {
        if (-not (Test-Path $path)) { throw "语料目录不存在：$path" }
    }
    New-Item -ItemType Directory -Force -Path $Output | Out-Null
    Write-Host "语料：$($Corpus -join '，')"
    Write-Host "门禁：$Gates"

    $invariantStatus = @{}
    $gateStatus = @{}

    if (-not $SkipEvaluation) {
        Write-Host "== 2. 三档 Hardware Profile 的评测运行 =="
        foreach ($profile in $profiles) {
            $profileDirectory = Join-Path $Output $profile
            New-Item -ItemType Directory -Force -Path $profileDirectory | Out-Null
            $arguments = @(
                "tools\evaluate.py",
                "--hardware-profile", $profile,
                "--runs-dir", $RunsDir,
                "--no-gates"
            )
            foreach ($path in $Corpus) { $arguments += @("--corpus", $path) }
            Write-Host "-- $profile"
            $result = Invoke-Python -Arguments $arguments -AllowFailure
            $manifest = Get-RunManifest -Directory $RunsDir -Output $result.text
            if (-not $manifest) { throw "$profile：$RunsDir 下没有这一轮的 run.json，评测没有落盘" }
            Copy-Item -LiteralPath $manifest -Destination (Join-Path $profileDirectory "run.json") -Force
            $invariantStatus[$profile] = $result.code
            Write-Host "   Run Manifest：$manifest"
            Write-Host "   invariant 退出码：$($result.code)"
        }
    }

    Write-Host "== 3. 逐档门禁判定 =="
    foreach ($profile in $profiles) {
        $profileDirectory = Join-Path $Output $profile
        $manifest = Join-Path $profileDirectory "run.json"
        if (-not (Test-Path $manifest)) {
            Write-Host "-- $profile：没有 Run Manifest，跳过门禁判定"
            $gateStatus[$profile] = "skipped"
            continue
        }
        $previous = Join-Path $profileDirectory "previous-run.json"
        $arguments = @(
            "tools\judge_gates.py",
            "--run", $manifest,
            "--gates", $Gates,
            "--json-out", (Join-Path $profileDirectory "gates.json")
        )
        if (Test-Path $previous) {
            $arguments += @("--baseline", $previous)
            Write-Host "-- $profile（基线：$previous）"
        }
        else {
            Write-Host "-- $profile（无上一轮基线，相对回退限制不判定）"
        }
        $result = Invoke-Python -Arguments $arguments -AllowFailure
        Copy-Item -LiteralPath $manifest -Destination $previous -Force
        $gateStatus[$profile] = $result.code
        Write-Host "   门禁退出码：$($result.code)，判定在 $profileDirectory\gates.json"
    }

    Write-Host "== 4. 汇总 =="
    $failedInvariants = @($invariantStatus.GetEnumerator() | Where-Object { $_.Value -ne 0 })
    $failedGates = @(
        $gateStatus.GetEnumerator() |
            Where-Object { $_.Value -is [int] -and $_.Value -ne 0 }
    )
    $skippedGates = @(
        $gateStatus.GetEnumerator() | Where-Object { $_.Value -eq "skipped" }
    )
    foreach ($profile in $profiles) {
        $invariants = if ($invariantStatus.ContainsKey($profile)) { $invariantStatus[$profile] } else { "—" }
        $gates = if ($gateStatus.ContainsKey($profile)) { $gateStatus[$profile] } else { "—" }
        Write-Host ("{0,-12} invariant={1} gates={2}" -f $profile, $invariants, $gates)
    }
    if ($skippedGates.Count -gt 0) {
        Write-Host "未判定档位（没有 Run Manifest，门禁没判）：$($skippedGates.Name -join '，')"
    }
    Write-Host "Run Manifest 与报告：$RunsDir；每档副本与门禁判定：$Output"

    if ($failedInvariants.Count -gt 0) {
        throw "invariant 失败：$($failedInvariants.Name -join '，')"
    }
    if ($failedGates.Count -gt 0) {
        $names = $failedGates.Name -join "，"
        if ($RequireGates) {
            throw "门禁失败：$names（未装 OCR 引擎时 OCR 层失败属预期，见 evaluation/quality-gates.md）"
        }
        Write-Host "门禁失败：$names。逐条原因见各档 gates.json（-RequireGates 会让脚本以此退出码结束）。"
    }
    Write-Host "分层基线完成。"
    # The contract is "invariant violations fail, and so does -RequireGates";
    # a gate failure on its own does not, so a caller reading $LASTEXITCODE must
    # not be left holding the exit code of the last gate judgement.
    $global:LASTEXITCODE = 0
}
finally {
    Pop-Location
}
