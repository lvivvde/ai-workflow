[CmdletBinding()]
param(
    [string]$Source = "examples\sample-corpus",
    [string]$Output = ".index\knowledge",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

foreach ($commandName in @("python", "uv", "git")) {
    if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
        throw "Missing prerequisite: $commandName. Install it manually, then rerun this script."
    }
}

Push-Location $projectRoot
try {
    $pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $versionParts = $pythonVersion.Split(".")
    if ([int]$versionParts[0] -lt 3 -or ([int]$versionParts[0] -eq 3 -and [int]$versionParts[1] -lt 10)) {
        throw "Python 3.10 or newer is required; found $pythonVersion"
    }

    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync --locked failed" }

    if (-not $SkipTests) {
        & uv run python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "Test suite failed; index was not built" }
    }

    & uv run game-design-knowledge index $Source --output $Output
    if ($LASTEXITCODE -ne 0) { throw "Index build failed" }

    $serverPath = (Resolve-Path ".venv\Scripts\game-design-knowledge-mcp.exe").Path
    $indexPath = (Resolve-Path $Output).Path
    $configuration = [ordered]@{
        mcpServers = [ordered]@{
            "game-design-knowledge" = [ordered]@{
                command = $serverPath
                env = [ordered]@{
                    GAME_DESIGN_INDEX_DIR = $indexPath
                }
            }
        }
    }

    Write-Host "`nDeployment succeeded. Paste this JSON into the MCP client configuration:"
    $json = $configuration | ConvertTo-Json -Depth 6
    $asciiJson = -join ($json.ToCharArray() | ForEach-Object {
        if ([int]$_ -gt 127) { "\u{0:x4}" -f [int]$_ } else { [string]$_ }
    })
    Write-Output $asciiJson
}
finally {
    Pop-Location
}
